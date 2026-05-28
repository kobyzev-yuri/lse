#!/usr/bin/env python3
"""
Train CatBoostClassifier on LLM scenario labels (final_label from apply_earnings_scenario_labels).

Target: final_label where label_source=llm_scenario_v0 (excludes UP/DOWN/FLAT rule labels).
Features: quotes_regime_earnings_v1 (peer graph + earnings tone + quotes + regime).

Examples:
  python scripts/train_event_reaction_scenario_classifier.py --dry-run
  python scripts/train_event_reaction_scenario_classifier.py --feature-builder-version quotes_regime_earnings_v1
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MODEL_VERSION = "event_reaction_scenario_v0"
LABEL_SOURCE = "llm_scenario_v0"
RULE_LABELS = frozenset({"UP", "DOWN", "FLAT"})

from config_loader import get_config_value  # noqa: E402
from report_generator import get_engine  # noqa: E402
from services.event_reaction_labeling import (  # noqa: E402
    FEATURE_BUILDER_VERSION_EARNINGS,
    FEATURE_BUILDER_VERSION_QUOTES,
    event_reaction_numeric_feature_keys,
)


def _json_obj(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def load_scenario_training_frame(
    engine,
    *,
    dataset_version: str,
    feature_builder_version: str,
) -> pd.DataFrame:
    from sqlalchemy import text

    numeric_keys = event_reaction_numeric_feature_keys(feature_builder_version)
    quote_keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_QUOTES)
    extra_keys = tuple(k for k in numeric_keys if k not in quote_keys)

    q = text(
        """
        SELECT id, symbol, event_time_et, final_label, label_source, features_before
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND final_label IS NOT NULL
          AND TRIM(final_label) <> ''
          AND label_source = :label_source
          AND features_before IS NOT NULL AND features_before <> '{}'::jsonb
          AND (features_before->>'feature_builder_version') = :fbv
        ORDER BY event_time_et NULLS LAST, id
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            q,
            conn,
            params={"dv": dataset_version, "fbv": feature_builder_version, "label_source": LABEL_SOURCE},
        )
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        label = str(r.get("final_label") or "").strip()
        if not label or label in RULE_LABELS:
            continue
        fb = _json_obj(r.get("features_before"))
        rec: Dict[str, Any] = {
            "id": int(r["id"]),
            "symbol": str(r["symbol"]).strip().upper(),
            "event_time_et": r.get("event_time_et"),
            "target_scenario": label,
        }
        skip = False
        for k in quote_keys:
            try:
                fv = float(fb.get(k))
            except (TypeError, ValueError):
                skip = True
                break
            if not math.isfinite(fv):
                skip = True
                break
            rec[k] = fv
        if skip:
            continue
        for k in extra_keys:
            try:
                fv = float(fb.get(k)) if fb.get(k) is not None else 0.0
            except (TypeError, ValueError):
                fv = 0.0
            if not math.isfinite(fv):
                fv = 0.0
            rec[k] = fv
        rows.append(rec)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser(description="Train event-reaction scenario CatBoost classifier")
    ap.add_argument("--dataset-version", default="", help="default EVENT_REACTION_DATASET_VERSION")
    ap.add_argument("--feature-builder-version", default="", help="default quotes_regime_earnings_v1")
    ap.add_argument("--min-rows", type=int, default=8)
    ap.add_argument("--valid-ratio", type=float, default=0.25)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--json-metrics-out", default="")
    args = ap.parse_args()

    ds_version = (
        args.dataset_version.strip()
        or (get_config_value("EVENT_REACTION_DATASET_VERSION", "") or "").strip()
        or "v0_expanded_baseline"
    )
    fbv = (
        args.feature_builder_version.strip()
        or (get_config_value("EVENT_REACTION_FEATURE_BUILDER_VERSION", "") or "").strip()
        or FEATURE_BUILDER_VERSION_EARNINGS
    )
    engine = get_engine()
    frame = load_scenario_training_frame(engine, dataset_version=ds_version, feature_builder_version=fbv)
    n_total = len(frame)
    min_required = max(8, int(args.min_rows))
    logger.info("Scenario train rows=%s fbv=%s min_required=%s", n_total, fbv, min_required)

    if n_total < min_required:
        logger.error("Insufficient scenario labels (%s < %s). Run apply_earnings_scenario_labels.py first.", n_total, min_required)
        return 1

    classes = sorted(frame["target_scenario"].unique().tolist())
    logger.info("Classes (%s): %s", len(classes), classes)

    feature_names = list(event_reaction_numeric_feature_keys(fbv)) + ["symbol"]
    frame = frame.sort_values(["event_time_et", "id"]).reset_index(drop=True)
    n_valid = max(1, int(math.ceil(n_total * float(args.valid_ratio))))
    n_train = n_total - n_valid
    if n_train < 1:
        n_train = n_total - 1
        n_valid = 1

    train_df = frame.iloc[:n_train]
    valid_df = frame.iloc[n_train:]
    X_train = train_df[feature_names]
    y_train = train_df["target_scenario"]
    X_valid = valid_df[feature_names]
    y_valid = valid_df["target_scenario"]

    metrics: Dict[str, Any] = {
        "n_train": int(n_train),
        "n_valid": int(n_valid),
        "n_total": int(n_total),
        "classes": classes,
        "feature_builder_version": fbv,
    }

    if args.dry_run:
        logger.info("dry-run: would train classifier on %s rows, valid=%s", n_train, n_valid)
        print(json.dumps({"status": "dry_run", **metrics}, ensure_ascii=False, indent=2))
        return 0

    from catboost import CatBoostClassifier, Pool

    cat_idx = [feature_names.index("symbol")]
    train_pool = Pool(X_train, y_train, cat_features=cat_idx, feature_names=feature_names)
    valid_pool = Pool(X_valid, y_valid, cat_features=cat_idx, feature_names=feature_names)

    model = CatBoostClassifier(
        iterations=300,
        learning_rate=0.08,
        depth=4,
        loss_function="MultiClass",
        random_seed=42,
        verbose=False,
        early_stopping_rounds=40,
    )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
    pred = model.predict(valid_pool)
    acc = float(np.mean(pred.reshape(-1) == y_valid.to_numpy()))
    metrics["valid_accuracy"] = round(acc, 4)

    out_final = args.out.strip() or (
        "/app/logs/ml/models/event_reaction_scenario_catboost.cbm"
        if Path("/app/logs").exists()
        else str(project_root / "local/models/event_reaction_scenario_catboost.cbm")
    )
    Path(out_final).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(out_final)
    logger.info("Saved scenario classifier to %s valid_accuracy=%.4f", out_final, acc)

    blob = {
        "script": "train_event_reaction_scenario_classifier",
        "status": "ok",
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "out_model_path": out_final,
        "metrics": metrics,
    }
    out_json = args.json_metrics_out.strip() or str(Path(out_final).with_suffix(".metrics.json"))
    Path(out_json).write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote metrics %s", out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

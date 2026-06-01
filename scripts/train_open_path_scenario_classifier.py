#!/usr/bin/env python3
"""
Train CatBoostClassifier on rule open-path scenario labels (pre-open features).

Examples:
  python scripts/train_open_path_scenario_classifier.py --dry-run
  python scripts/train_open_path_scenario_classifier.py --since 2026-01-01
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

from services.open_path_classifier_dataset import (  # noqa: E402
    FEATURE_BUILDER_VERSION,
    MODEL_VERSION,
    load_open_path_training_frame,
    open_path_numeric_feature_keys,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Train open-path scenario CatBoost classifier")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--min-rows", type=int, default=0, help="0 = use ML_READINESS_OPEN_PATH_CLASSIFIER_MIN_TRAIN")
    ap.add_argument("--valid-ratio", type=float, default=0.25)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--json-metrics-out", default="")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine

    min_required = int(args.min_rows)
    if min_required <= 0:
        min_required = int((get_config_value("ML_READINESS_OPEN_PATH_CLASSIFIER_MIN_TRAIN", "120") or "120").strip())

    engine = get_engine()
    frame = load_open_path_training_frame(engine, since=args.since.strip())
    n_total = len(frame)
    logger.info("Open-path train rows=%s min_required=%s", n_total, min_required)

    if n_total < max(8, min_required):
        logger.error("Insufficient open-path labels (%s < %s). Run label_open_path_scenarios.py first.", n_total, min_required)
        blob = {
            "status": "insufficient_rows",
            "script": "train_open_path_scenario_classifier",
            "n_total": n_total,
            "min_required": min_required,
        }
        if args.json_metrics_out.strip():
            Path(args.json_metrics_out.strip()).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_metrics_out.strip()).write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    classes = sorted(frame["target_scenario"].unique().tolist())
    feature_names = list(open_path_numeric_feature_keys()) + ["symbol"]
    frame = frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    n_valid = max(1, int(math.ceil(n_total * float(args.valid_ratio))))
    n_train = n_total - n_valid
    if n_train < 1:
        n_train = n_total - 1
        n_valid = 1

    train_df = frame.iloc[:n_train]
    valid_df = frame.iloc[n_train:]
    holdout_skipped = False
    unseen_valid_classes: list[str] = []
    unseen_valid = set(valid_df["target_scenario"].unique()) - set(train_df["target_scenario"].unique())
    if unseen_valid:
        logger.warning(
            "Valid holdout has classes absent from train (%s); training on full sample without eval_set",
            sorted(unseen_valid),
        )
        train_df = frame
        valid_df = frame.iloc[0:0]
        n_train, n_valid = int(n_total), 0
        holdout_skipped = True
        unseen_valid_classes = sorted(unseen_valid)

    metrics: Dict[str, Any] = {
        "n_train": int(n_train),
        "n_valid": int(n_valid),
        "n_total": int(n_total),
        "classes": classes,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "holdout_skipped": holdout_skipped,
        "unseen_valid_classes": unseen_valid_classes,
    }

    if args.dry_run:
        blob = {"status": "dry_run", "script": "train_open_path_scenario_classifier", **metrics}
        if args.json_metrics_out.strip():
            Path(args.json_metrics_out.strip()).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_metrics_out.strip()).write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(blob, ensure_ascii=False, indent=2))
        return 0

    from catboost import CatBoostClassifier, Pool

    X_train = train_df[feature_names]
    y_train = train_df["target_scenario"]
    X_valid = valid_df[feature_names]
    y_valid = valid_df["target_scenario"]
    cat_idx = [feature_names.index("symbol")]
    train_pool = Pool(X_train, y_train, cat_features=cat_idx, feature_names=feature_names)

    model = CatBoostClassifier(
        iterations=400,
        learning_rate=0.06,
        depth=5,
        loss_function="MultiClass",
        random_seed=42,
        verbose=False,
        early_stopping_rounds=50,
    )
    if n_valid > 0:
        valid_pool = Pool(X_valid, y_valid, cat_features=cat_idx, feature_names=feature_names)
        model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
        pred = model.predict(valid_pool)
        acc = float(np.mean(pred.reshape(-1) == y_valid.to_numpy()))
        metrics["valid_accuracy"] = round(acc, 4)
    else:
        model.fit(train_pool)
        metrics["valid_accuracy"] = None
        acc = float("nan")

    out_final = args.out.strip() or (
        "/app/logs/ml/models/open_path_scenario_catboost.cbm"
        if Path("/app/logs").exists()
        else str(project_root / "local/models/open_path_scenario_catboost.cbm")
    )
    Path(out_final).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(out_final)
    logger.info("Saved open-path classifier to %s valid_accuracy=%s", out_final, metrics.get("valid_accuracy"))

    blob = {
        "script": "train_open_path_scenario_classifier",
        "status": "ok",
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "out_model_path": out_final,
        "metrics": metrics,
    }
    out_json = args.json_metrics_out.strip() or str(Path(out_final).with_suffix(".metrics.json"))
    Path(out_json).write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

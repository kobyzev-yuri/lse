#!/usr/bin/env python3
"""
Train CatBoostRegressor: (source_event, peer) → peer_forward_log_ret_5d.

Features: edge weight + source quotes_regime_earnings_v1 + source/peer/relation cats.
Target: peer_forward_log_ret_5d (log-space, transaction costs not in y).

Examples:
  python scripts/train_peer_spillover_regressor.py --dry-run
  python scripts/train_peer_spillover_regressor.py --json-metrics-out logs/ml/ml_data_quality/last_peer_spillover_train_metrics.json
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

MODEL_VERSION = "peer_spillover_forward5d_v0"

from config_loader import get_config_value  # noqa: E402
from report_generator import get_engine  # noqa: E402
from services.event_reaction_labeling import FEATURE_BUILDER_VERSION_EARNINGS  # noqa: E402
from services.peer_spillover_dataset import (  # noqa: E402
    load_peer_spillover_training_frame,
    peer_spillover_categorical_features,
    peer_spillover_feature_names,
    summarize_peer_spillover_rows,
)


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.sqrt(np.mean((y_pred - y_true) ** 2)))


def _sign_acc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.mean((y_true >= 0) == (y_pred >= 0)))


def _default_model_path() -> str:
    if Path("/app/logs").exists():
        return "/app/logs/ml/models/peer_spillover_forward5d_catboost.cbm"
    return str(project_root / "local/models/peer_spillover_forward5d_catboost.cbm")


def main() -> int:
    ap = argparse.ArgumentParser(description="Train peer spillover CatBoost regressor")
    ap.add_argument("--dataset-version", default="", help="default EVENT_REACTION_DATASET_VERSION")
    ap.add_argument("--feature-builder-version", default="", help="default quotes_regime_earnings_v1")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--min-rows", type=int, default=20)
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
    fbv = args.feature_builder_version.strip() or FEATURE_BUILDER_VERSION_EARNINGS
    min_required = max(12, int(args.min_rows))

    engine = get_engine()
    frame = load_peer_spillover_training_frame(
        engine,
        dataset_version=ds_version,
        feature_builder_version=fbv,
        since=args.since.strip(),
        limit=args.limit,
    )
    n_total = len(frame)
    logger.info("Peer spillover train rows=%s fbv=%s min_required=%s", n_total, fbv, min_required)

    dataset_summary = summarize_peer_spillover_rows(
        [
            {
                "source_symbol": r["source_symbol"],
                "event_date": r["event_date"],
                "peer_ticker": r["peer_ticker"],
                "peer_forward_log_ret_5d": r["target_peer_forward_log_ret_5d"],
                "source_forward_log_ret_5d": r["source_forward_log_ret_5d"],
                "baseline_propagation_log": r["baseline_propagation_log"],
            }
            for _, r in frame.iterrows()
        ]
    )

    metrics: Dict[str, Any] = {
        "n_total": int(n_total),
        "feature_builder_version": fbv,
        "dataset_summary": dataset_summary,
    }

    if n_total < min_required:
        logger.error("Insufficient peer spillover rows (%s < %s)", n_total, min_required)
        blob = {
            "script": "train_peer_spillover_regressor",
            "status": "insufficient_rows",
            "metrics": metrics,
        }
        if args.json_metrics_out.strip():
            Path(args.json_metrics_out.strip()).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_metrics_out.strip()).write_text(
                json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 1

    feature_names = peer_spillover_feature_names(feature_builder_version=fbv)
    frame = frame.sort_values(["event_date", "dataset_id", "peer_ticker"]).reset_index(drop=True)
    n_valid = max(1, int(math.ceil(n_total * float(args.valid_ratio))))
    n_train = n_total - n_valid
    if n_train < 1:
        n_train = n_total - 1
        n_valid = 1

    train_df = frame.iloc[:n_train]
    valid_df = frame.iloc[n_train:]
    metrics.update({"n_train": int(n_train), "n_valid": int(n_valid)})

    if args.dry_run:
        logger.info("dry-run: would train peer spillover on %s rows, valid=%s", n_train, n_valid)
        blob = {"status": "dry_run", "script": "train_peer_spillover_regressor", "metrics": metrics}
        if args.json_metrics_out.strip():
            Path(args.json_metrics_out.strip()).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_metrics_out.strip()).write_text(
                json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(json.dumps(blob, ensure_ascii=False, indent=2))
        return 0

    from catboost import CatBoostRegressor, Pool

    y_train = train_df["target_peer_forward_log_ret_5d"]
    y_valid = valid_df["target_peer_forward_log_ret_5d"]
    X_train = train_df[feature_names]
    X_valid = valid_df[feature_names]
    cat_idx = [feature_names.index(c) for c in peer_spillover_categorical_features()]
    train_pool = Pool(X_train, y_train, cat_features=cat_idx, feature_names=feature_names)

    model = CatBoostRegressor(
        iterations=400,
        learning_rate=0.06,
        depth=5,
        loss_function="RMSE",
        random_seed=42,
        verbose=False,
        early_stopping_rounds=50,
    )
    if n_valid > 0:
        valid_pool = Pool(X_valid, y_valid, cat_features=cat_idx, feature_names=feature_names)
        model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
        pred = model.predict(valid_pool).reshape(-1)
        yv = y_valid.to_numpy()
        metrics["rmse_valid"] = round(_rmse(yv, pred), 6)
        metrics["sign_accuracy_valid"] = round(_sign_acc(yv, pred), 4)
        baseline = valid_df["baseline_propagation_log"].to_numpy()
        metrics["baseline_sign_accuracy_valid"] = round(_sign_acc(yv, baseline), 4)
        metrics["source_sign_accuracy_valid"] = round(
            _sign_acc(yv, valid_df["source_forward_log_ret_5d"].to_numpy()), 4
        )
    else:
        model.fit(train_pool)
        metrics["rmse_valid"] = None
        metrics["sign_accuracy_valid"] = None

    out_final = args.out.strip() or _default_model_path()
    Path(out_final).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(out_final)
    logger.info(
        "Saved peer spillover model to %s rmse_valid=%s sign_acc=%s",
        out_final,
        metrics.get("rmse_valid"),
        metrics.get("sign_accuracy_valid"),
    )

    meta_path = str(Path(out_final).with_suffix(".meta.json"))
    Path(meta_path).write_text(
        json.dumps(
            {
                "model_version": MODEL_VERSION,
                "feature_names": feature_names,
                "categorical_features": list(peer_spillover_categorical_features()),
                "feature_builder_version": fbv,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    blob = {
        "script": "train_peer_spillover_regressor",
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

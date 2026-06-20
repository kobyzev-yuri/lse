#!/usr/bin/env python3
"""
Train CatBoost on GAME_5M continuation dataset (TAKE exits → label_missed_upside).

  python scripts/train_game5m_continuation_catboost.py \
    --csv /app/logs/ml/datasets/game5m_continuation_dataset.csv

See docs/GAME_5M_PREDICTOR_DATASET_PLAN.md phase 2.2.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_metrics(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    mp = Path(path)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Train CatBoost continuation model from TAKE dataset CSV")
    parser.add_argument("--csv", type=str, default="", help="Continuation dataset CSV")
    parser.add_argument("--min-rows", type=int, default=None, help="Override GAME_5M_CONTINUATION_ML_MIN_TRAIN_ROWS")
    parser.add_argument("--valid-ratio", type=float, default=0.2, help="Last fraction for validation (time-ordered)")
    parser.add_argument("--out", type=str, default="", help="Output .cbm path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-metrics-out", type=str, default="")
    args = parser.parse_args()

    try:
        from catboost import CatBoostClassifier, Pool
    except ImportError:
        logger.error("Install catboost: pip install -r requirements-catboost.txt")
        return 1

    from config_loader import get_config_value
    from services.game5m_continuation_catboost import (
        continuation_catboost_schema,
        default_continuation_catboost_model_path,
    )
    from services.game5m_continuation_dataset import (
        CONTINUATION_ML_MIN_TAKE_ROWS_DEFAULT,
        CONTINUATION_ML_SCHEMA_VERSION,
        default_continuation_dataset_csv_path,
        row_from_continuation_dataset_dict,
    )

    csv_arg = (args.csv or "").strip() or (get_config_value("GAME_5M_CONTINUATION_DATASET_CSV", "") or "").strip()
    if not csv_arg:
        csv_arg = default_continuation_dataset_csv_path()
    csv_path = Path(csv_arg).expanduser()
    if not csv_path.is_file():
        logger.error("CSV not found: %s", csv_path)
        return 1

    out_arg = (args.out or "").strip() or (get_config_value("GAME_5M_CONTINUATION_CATBOOST_MODEL_PATH", "") or "").strip()
    out_final = out_arg or str(default_continuation_catboost_model_path())

    min_rows = args.min_rows
    if min_rows is None:
        try:
            min_rows = int((get_config_value("GAME_5M_CONTINUATION_ML_MIN_TRAIN_ROWS", str(CONTINUATION_ML_MIN_TAKE_ROWS_DEFAULT)) or str(CONTINUATION_ML_MIN_TAKE_ROWS_DEFAULT)).strip())
        except (ValueError, TypeError):
            min_rows = CONTINUATION_ML_MIN_TAKE_ROWS_DEFAULT
    min_rows = max(20, min_rows)

    raw_rows = _read_csv(csv_path)
    packed: list[tuple[str, list[Any], int]] = []
    for rec in raw_rows:
        xv = row_from_continuation_dataset_dict(rec)
        if xv is None:
            continue
        try:
            y = int(rec.get("label_missed_upside") or 0)
        except (TypeError, ValueError):
            continue
        if y not in (0, 1):
            continue
        ts_key = str(rec.get("exit_ts_et") or rec.get("entry_ts_et") or "")
        packed.append((ts_key, xv, y))

    packed.sort(key=lambda t: t[0])
    rows = [t[1] for t in packed]
    labels = [t[2] for t in packed]
    n_total = len(rows)
    pos = sum(labels)
    logger.info("Continuation CSV %s: rows=%s (y=1: %s, y=0: %s)", csv_path, n_total, pos, n_total - pos)

    if n_total < min_rows:
        logger.warning("Rows below threshold %s — model not written.", min_rows)
        _write_metrics(
            args.json_metrics_out,
            {
                "script": "train_game5m_continuation_catboost",
                "status": "insufficient_rows",
                "schema_version": CONTINUATION_ML_SCHEMA_VERSION,
                "n_total": n_total,
                "y_pos": pos,
                "min_train_rows_config": min_rows,
                "csv_path": str(csv_path),
            },
        )
        return 2

    n_valid = max(1, int(n_total * float(args.valid_ratio)))
    n_train = n_total - n_valid
    if n_train < 10:
        n_train = max(10, n_total // 2)
        n_valid = n_total - n_train

    feature_names, cat_features = continuation_catboost_schema()
    train_pool = Pool(rows[:n_train], label=labels[:n_train], cat_features=cat_features, feature_names=feature_names)
    valid_pool = Pool(rows[n_train:], label=labels[n_train:], cat_features=cat_features, feature_names=feature_names)

    scale_pos_weight = (n_total - pos) / max(pos, 1) if 0 < pos < n_total else 1.0
    model = CatBoostClassifier(
        iterations=400,
        learning_rate=0.05,
        depth=6,
        loss_function="Logloss",
        eval_metric="AUC",
        random_seed=42,
        verbose=False,
        early_stopping_rounds=50,
        scale_pos_weight=float(scale_pos_weight),
    )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)

    try:
        from sklearn.metrics import roc_auc_score

        proba = model.predict_proba(rows[n_train:])[:, 1]
        valid_y = labels[n_train:]
        auc = roc_auc_score(valid_y, proba) if len(set(valid_y)) > 1 else float("nan")
    except Exception:
        auc = float("nan")
    logger.info("Continuation Train=%s Valid=%s AUC(valid)≈%s", n_train, n_valid, auc if auc == auc else "n/a")

    out_path = Path(out_final)
    meta_path = out_path.with_suffix(".meta.json")
    meta = {
        "script": "train_game5m_continuation_catboost",
        "schema_version": CONTINUATION_ML_SCHEMA_VERSION,
        "status": "ok",
        "dry_run": bool(args.dry_run),
        "feature_names": feature_names,
        "cat_feature_indices": cat_features,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": n_train,
        "n_valid": n_valid,
        "n_total": n_total,
        "y_pos": pos,
        "label": "label_missed_upside",
        "min_train_rows_config": min_rows,
        "auc_valid": round(auc, 4) if auc == auc else None,
        "csv_path": str(csv_path.resolve()),
        "promotion_note": "Shadow only until AUC valid >= promotion gate (default 0.55) and ops sign-off.",
    }
    _write_metrics(args.json_metrics_out, meta)

    if args.dry_run:
        logger.info("Dry-run: not writing %s", out_path)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))
    meta_save = {k: v for k, v in meta.items() if k not in ("script", "status", "dry_run")}
    meta_path.write_text(json.dumps(meta_save, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved: %s and %s", out_path, meta_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

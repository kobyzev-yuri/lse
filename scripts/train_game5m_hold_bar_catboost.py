#!/usr/bin/env python3
"""
Train CatBoost on unified hold-bar CSV (exit/hold bake-off B2).

  python scripts/train_game5m_hold_bar_catboost.py \\
    --csv local/datasets/game5m_hold_bar_dataset.csv \\
    --feature-mode full

See docs/GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Train hold-bar CatBoost (y_hold_good)")
    ap.add_argument("--csv", required=True, help="CSV from build_game5m_hold_bar_dataset.py")
    ap.add_argument("--feature-mode", choices=("recovery", "full"), default="full")
    ap.add_argument("--valid-ratio", type=float, default=0.2)
    ap.add_argument("--out", type=str, default="")
    ap.add_argument("--json-metrics-out", type=str, default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        from catboost import CatBoostClassifier, Pool
        from sklearn.metrics import roc_auc_score
    except ImportError:
        logger.error("pip install -r requirements-catboost.txt (catboost, sklearn)")
        return 1

    from services.game5m_hold_bar_dataset import (
        FeatureMode,
        get_hold_bar_train_feature_schema,
        row_from_hold_bar_dict,
        y_hold_good_from_row,
    )

    path = Path(args.csv).expanduser()
    if not path.is_file():
        logger.error("CSV not found: %s", path)
        return 1

    mode: FeatureMode = "recovery" if args.feature_mode == "recovery" else "full"
    rows: list[list] = []
    labels: list[int] = []
    bar_ts: list[str] = []

    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            y = y_hold_good_from_row(raw)
            if y is None:
                continue
            vec = row_from_hold_bar_dict(raw, mode=mode)
            if not vec[0]:
                continue
            rows.append(vec)
            labels.append(int(y))
            bar_ts.append(str(raw.get("bar_ts_et") or ""))

    n_total = len(rows)
    if n_total < 50:
        logger.error("insufficient rows: %d", n_total)
        return 1

    order = sorted(range(n_total), key=lambda i: bar_ts[i] or "")
    n_valid = max(1, int(n_total * float(args.valid_ratio)))
    n_train = n_total - n_valid
    train_idx = set(order[:n_train])
    train_rows = [rows[i] for i in range(n_total) if i in train_idx]
    train_labels = [labels[i] for i in range(n_total) if i in train_idx]
    valid_rows = [rows[i] for i in range(n_total) if i not in train_idx]
    valid_labels = [labels[i] for i in range(n_total) if i not in train_idx]

    feature_names, cat_features = get_hold_bar_train_feature_schema(mode)
    train_pool = Pool(train_rows, label=train_labels, cat_features=cat_features, feature_names=feature_names)
    valid_pool = Pool(valid_rows, label=valid_labels, cat_features=cat_features, feature_names=feature_names)

    if args.dry_run:
        logger.info("dry-run rows=%d train=%d valid=%d features=%d", n_total, len(train_rows), len(valid_rows), len(feature_names))
        return 0

    pos = sum(train_labels)
    neg = len(train_labels) - pos
    model = CatBoostClassifier(
        iterations=400,
        depth=6,
        learning_rate=0.05,
        loss_function="Logloss",
        eval_metric="AUC",
        random_seed=42,
        verbose=False,
        scale_pos_weight=neg / max(pos, 1),
    )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
    proba = model.predict_proba(valid_pool)[:, 1]
    try:
        auc = float(roc_auc_score(valid_labels, proba)) if len(set(valid_labels)) > 1 else float("nan")
    except Exception:
        auc = float("nan")

    out_final = (args.out or "").strip() or str(project_root / "local" / "models" / "game5m_hold_bar_catboost.cbm")
    out_path = Path(out_final).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "script": "train_game5m_hold_bar_catboost",
        "feature_mode": mode,
        "feature_names": feature_names,
        "cat_features": cat_features,
        "label_column": "y_hold_good",
        "n_train": len(train_rows),
        "n_valid": len(valid_rows),
        "auc_valid": round(auc, 4) if auc == auc else None,
        "hold_csv": str(path),
    }
    meta_path = out_path.with_suffix(".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    metrics_path = (args.json_metrics_out or "").strip()
    if metrics_path:
        mp = Path(metrics_path).expanduser()
        mp.parent.mkdir(parents=True, exist_ok=True)
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    logger.info("saved %s auc_valid=%s", out_path, meta["auc_valid"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

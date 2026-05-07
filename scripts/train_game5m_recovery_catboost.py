#!/usr/bin/env python3
"""
Обучение CatBoostClassifier по JSONL экспорту recovery (фаза C/D).

  pip install -r requirements-catboost.txt
  python scripts/train_game5m_recovery_catboost.py --jsonl /path/to/export.jsonl [--horizon 120]

См. docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md (фаза D1).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _bar_ts_key(rec: Dict[str, Any]) -> Tuple[int, str]:
    ts = str(rec.get("bar_ts_et") or "")
    tid = 0
    try:
        tid = int(rec.get("trade_id") or 0)
    except (TypeError, ValueError):
        tid = 0
    return (tid, ts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Train CatBoost recovery model from analyzer JSONL export")
    parser.add_argument("--jsonl", type=str, default="", help="Path to JSONL from export_recovery_ml")
    parser.add_argument(
        "--horizon",
        type=int,
        default=120,
        help="Label column h{H}_y_recovery (must exist in each row)",
    )
    parser.add_argument("--min-rows", type=int, default=None, help="Override GAME_5M_RECOVERY_ML_MIN_TRAIN_ROWS")
    parser.add_argument("--valid-ratio", type=float, default=0.2, help="Last fraction of rows for validation (time-ordered)")
    parser.add_argument("--out", type=str, default="", help="Output .cbm path (meta alongside .meta.json)")
    parser.add_argument("--dry-run", action="store_true", help="Only print stats, no model file")
    args = parser.parse_args()

    try:
        from catboost import CatBoostClassifier, Pool
    except ImportError:
        logger.error("Установите catboost: pip install -r requirements-catboost.txt")
        return 1

    from config_loader import get_config_value
    from services.game5m_recovery_catboost import (
        default_recovery_catboost_model_path,
        recovery_catboost_schema,
        row_vector_from_export_record,
    )

    jsonl_arg = (args.jsonl or "").strip()
    if not jsonl_arg:
        jsonl_arg = (get_config_value("GAME_5M_RECOVERY_TRAIN_JSONL", "") or "").strip()
    if not jsonl_arg:
        logger.error("Укажите --jsonl или GAME_5M_RECOVERY_TRAIN_JSONL в config.env")
        return 1

    jsonl_path = Path(jsonl_arg).expanduser()
    if not jsonl_path.is_file():
        logger.error("Файл не найден: %s", jsonl_path)
        return 1

    out_arg = (args.out or "").strip()
    cfg_out = (get_config_value("GAME_5M_RECOVERY_CATBOOST_MODEL_PATH", "") or "").strip()
    if out_arg:
        out_final = out_arg
    elif cfg_out:
        out_final = cfg_out
    else:
        out_final = str(default_recovery_catboost_model_path())

    min_rows = args.min_rows
    if min_rows is None:
        try:
            min_rows = int((get_config_value("GAME_5M_RECOVERY_ML_MIN_TRAIN_ROWS", "400") or "400").strip())
        except (ValueError, TypeError):
            min_rows = 400
    min_rows = max(50, min_rows)

    H = int(args.horizon)
    y_key = f"h{H}_y_recovery"

    raw_rows = _read_jsonl(jsonl_path)
    packed: List[Tuple[Tuple[int, str], List[Any], int]] = []
    for rec in raw_rows:
        xv = row_vector_from_export_record(rec)
        if xv is None or y_key not in rec:
            continue
        try:
            y = int(rec[y_key])
        except (TypeError, ValueError):
            continue
        if y not in (0, 1):
            continue
        packed.append((_bar_ts_key(rec), xv, y))

    packed.sort(key=lambda t: t[0])
    X_list = [t[1] for t in packed]
    y_list = [t[2] for t in packed]

    n_total = len(X_list)
    pos = sum(y_list)
    logger.info(
        "Строк из %s с валидными X и %s: %s (y=1: %s, y=0: %s)",
        jsonl_path,
        y_key,
        n_total,
        pos,
        n_total - pos,
    )

    if n_total < min_rows:
        logger.warning(
            "Строк меньше порога %s — модель не пишем. Соберите больший экспорт или снизьте GAME_5M_RECOVERY_ML_MIN_TRAIN_ROWS.",
            min_rows,
        )
        return 2

    n_valid = max(1, int(n_total * float(args.valid_ratio)))
    n_train = n_total - n_valid
    if n_train < 20:
        n_train = max(20, n_total // 2)
        n_valid = n_total - n_train

    train_X = X_list[:n_train]
    train_y = y_list[:n_train]
    valid_X = X_list[n_train:]
    valid_y = y_list[n_train:]

    feature_names, cat_features = recovery_catboost_schema()
    train_pool = Pool(train_X, label=train_y, cat_features=cat_features, feature_names=feature_names)
    valid_pool = Pool(valid_X, label=valid_y, cat_features=cat_features, feature_names=feature_names)

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

        proba = model.predict_proba(valid_X)[:, 1]
        auc = roc_auc_score(valid_y, proba) if len(set(valid_y)) > 1 else float("nan")
    except Exception:
        auc = float("nan")
    logger.info("Train=%s Valid=%s AUC(valid)≈%s", n_train, n_valid, auc if auc == auc else "n/a")

    out_path = Path(out_final)
    meta_path = out_path.with_suffix(".meta.json")

    if args.dry_run:
        logger.info("Dry-run: не записываем %s", out_path)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))

    meta = {
        "feature_names": feature_names,
        "cat_feature_indices": cat_features,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": n_train,
        "n_valid": n_valid,
        "n_total": n_total,
        "label_column": y_key,
        "horizon_minutes": H,
        "jsonl_source": str(jsonl_path.resolve()),
        "min_train_rows_config": min_rows,
        "auc_valid": round(auc, 4) if auc == auc else None,
        "positive_rate": round(pos / n_total, 4) if n_total else None,
        "schema_note": "Признаки как в services/game5m_recovery_catboost.py / RECOVERY_ML_SCHEMA",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info("Сохранено: %s и %s", out_path, meta_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

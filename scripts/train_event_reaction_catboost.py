#!/usr/bin/env python3
"""
MVP: CatBoostRegressor по строкам `event_reaction_dataset` (цена до события → forward 5d log-ret).

Признаки: плоские числовые поля из `features_before` (`quotes_mvp_1` или `quotes_regime_v1`), плюс `symbol`.
Цель: `outcomes_after.forward_log_ret_5d` (log-пространство).

  python scripts/train_event_reaction_catboost.py --dry-run --json-metrics-out local/logs/last_event_reaction_train_metrics.json

Порог строк: `--min-rows` (default 10), жёсткий минимум max(8, …); переопределение из config: **EVENT_REACTION_TRAIN_MIN_ROWS**.

См. docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md
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

MODEL_VERSION = "event_reaction_forward5d_v0"

from services.event_reaction_labeling import (  # noqa: E402
    FEATURE_BUILDER_VERSION_QUOTES,
    active_feature_builder_version,
    event_reaction_numeric_feature_keys,
    event_reaction_optional_quote_defaults,
    event_reaction_required_quote_keys,
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


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.sqrt(np.mean((y_pred - y_true) ** 2)))


def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.mean(np.abs(y_pred - y_true)))


def _rank_metrics(y_true: np.ndarray, y_pred: np.ndarray, threshold_log: float) -> Dict[str, Any]:
    if y_true.size == 0:
        return {}
    n_top = max(1, int(math.ceil(y_true.size * 0.1)))
    order = np.argsort(y_pred)[::-1]
    top = y_true[order[:n_top]]
    all_hit = float(np.mean(y_true > threshold_log))
    top_hit = float(np.mean(top > threshold_log))
    return {
        "top_decile_n": int(n_top),
        "top_decile_mean_log_return": float(np.mean(top)),
        "top_decile_mean_simple_pct": float((np.exp(np.mean(top)) - 1.0) * 100.0),
        "top_decile_hit_rate_pct": round(top_hit * 100.0, 2),
        "all_valid_hit_rate_pct": round(all_hit * 100.0, 2),
    }


def load_training_frame(
    engine,
    *,
    dataset_version: str,
    feature_builder_version: str,
) -> pd.DataFrame:
    numeric_keys = event_reaction_numeric_feature_keys(feature_builder_version)
    quote_required = event_reaction_required_quote_keys()
    quote_optional_defaults = event_reaction_optional_quote_defaults()
    all_quote_keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_QUOTES)
    quote_optional = tuple(k for k in all_quote_keys if k not in quote_required)
    regime_keys = tuple(k for k in numeric_keys if k not in all_quote_keys)
    from sqlalchemy import text

    q = text(
        """
        SELECT id, symbol, event_time_et, features_before, outcomes_after
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND features_before IS NOT NULL AND features_before <> '{}'::jsonb
          AND outcomes_after IS NOT NULL AND outcomes_after <> '{}'::jsonb
          AND outcomes_after ? 'forward_log_ret_5d'
          AND (features_before->>'feature_builder_version') = :fbv
        ORDER BY event_time_et NULLS LAST, id
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"dv": dataset_version, "fbv": feature_builder_version})
    if df.empty:
        return df
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        fb = _json_obj(r.get("features_before"))
        oa = _json_obj(r.get("outcomes_after"))
        try:
            y = float(oa.get("forward_log_ret_5d"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(y):
            continue
        rec: Dict[str, Any] = {
            "id": int(r["id"]),
            "symbol": str(r["symbol"]).strip().upper(),
            "event_time_et": r.get("event_time_et"),
            "target_log_ret_5d": y,
        }
        skip = False
        for k in quote_required:
            v = fb.get(k)
            try:
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                fv = float("nan")
            if not math.isfinite(fv):
                skip = True
                break
            rec[k] = fv
        if skip:
            continue
        for k in quote_optional:
            v = fb.get(k)
            try:
                fv = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                fv = float("nan")
            if not math.isfinite(fv):
                fv = float(quote_optional_defaults.get(k, 0.0))
            rec[k] = fv
        for k in regime_keys:
            v = fb.get(k)
            try:
                fv = float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                fv = 0.0
            if not math.isfinite(fv):
                fv = 0.0
            rec[k] = fv
        rows.append(rec)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Train event_reaction CatBoost (forward 5d log-ret)")
    parser.add_argument("--dataset-version", type=str, default="v0")
    parser.add_argument(
        "--feature-builder-version",
        type=str,
        default="",
        help="features_before.feature_builder_version (default: EVENT_REACTION_FEATURE_BUILDER_VERSION)",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=10,
        help="Минимум строк после фильтрации для обучения (жёсткий пол ≥8: max(8, min-rows))",
    )
    parser.add_argument("--valid-ratio", type=float, default=0.25, help="Доля хвоста по времени для valid")
    parser.add_argument("--dry-run", action="store_true", help="Не писать .cbm")
    parser.add_argument("--out", type=str, default="", help="Путь .cbm")
    parser.add_argument("--json-metrics-out", type=str, default="", help="JSON метрик для readiness / отчёта")
    args = parser.parse_args()

    def _write_metrics_json(path_str: str, payload: Dict[str, Any]) -> None:
        ps = (path_str or "").strip()
        if not ps:
            return
        outp = Path(ps).expanduser()
        outp.parent.mkdir(parents=True, exist_ok=True)
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    try:
        from catboost import CatBoostRegressor, Pool
    except ImportError:
        logger.error("Установите catboost: pip install -r requirements-catboost.txt")
        return 1

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.event_reaction_labeling import event_reaction_label_threshold_log

    fbv = (args.feature_builder_version or "").strip() or active_feature_builder_version()
    numeric_keys = event_reaction_numeric_feature_keys(fbv)
    cfg_mr = (get_config_value("EVENT_REACTION_TRAIN_MIN_ROWS", "") or "").strip()
    try:
        min_rows_eff = int(cfg_mr) if cfg_mr else int(args.min_rows)
    except (TypeError, ValueError):
        min_rows_eff = int(args.min_rows)
    # Не ниже 8 строк — иначе CatBoost/сплит нестабильны; верхний предел оставляем на усмотрение CLI.
    min_required = max(8, int(min_rows_eff))

    engine = get_engine()
    df = load_training_frame(engine, dataset_version=args.dataset_version.strip() or "v0", feature_builder_version=fbv)
    if df.empty:
        logger.error("Нет строк для обучения (проверьте разметку outcomes_after.forward_log_ret_5d и features_before).")
        _write_metrics_json(
            args.json_metrics_out,
            {
                "script": "train_event_reaction_catboost",
                "status": "no_dataset",
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "note": "empty_after_filters",
            },
        )
        return 2

    df = df.sort_values(["event_time_et", "id"], na_position="last").reset_index(drop=True)
    n_total = len(df)
    if n_total < min_required:
        logger.warning(
            "Строк %s < порога %s (config EVENT_REACTION_TRAIN_MIN_ROWS или --min-rows, минимум 8) — модель не пишем.",
            n_total,
            min_required,
        )
        _write_metrics_json(
            args.json_metrics_out,
            {
                "script": "train_event_reaction_catboost",
                "status": "insufficient_rows",
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "n_total": int(n_total),
                "min_rows_config": int(min_rows_eff),
                "min_rows_required": int(min_required),
                "n_train": 0,
                "n_valid": 0,
                "metrics": {},
            },
        )
        return 2

    feature_names = ["symbol"] + list(numeric_keys)
    cat_features = [0]
    X = df[feature_names].copy()
    y = df["target_log_ret_5d"].astype(float).to_numpy()

    n_valid = max(1, int(n_total * float(args.valid_ratio)))
    n_train = n_total - n_valid
    if n_total >= 20 and n_train < 15:
        n_train = max(15, n_total // 2)
        n_valid = n_total - n_train
    elif n_total < 20:
        # Малый датасет: не требуем 15 train — достаточно ≥2 train и ≥1 valid
        n_valid = max(1, min(n_valid, n_total - 2))
        n_train = n_total - n_valid
        if n_train < 2:
            n_train = max(1, n_total - 1)
            n_valid = n_total - n_train

    train_X = X.iloc[:n_train].values.tolist()
    valid_X = X.iloc[n_train:].values.tolist()
    train_y = y[:n_train]
    valid_y = y[n_train:]

    train_pool = Pool(train_X, label=train_y, cat_features=cat_features, feature_names=feature_names)
    valid_pool = Pool(valid_X, label=valid_y, cat_features=cat_features, feature_names=feature_names)

    model = CatBoostRegressor(
        iterations=400,
        learning_rate=0.05,
        depth=5,
        loss_function="RMSE",
        eval_metric="RMSE",
        random_seed=42,
        verbose=False,
        early_stopping_rounds=50,
    )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
    pred = np.asarray(model.predict(valid_pool), dtype=float)
    thr = event_reaction_label_threshold_log()
    metrics = {
        "rmse_valid": _rmse(valid_y, pred),
        "mae_valid": _mae(valid_y, pred),
        "mean_target_valid_log_return": float(np.mean(valid_y)) if valid_y.size else None,
        "threshold_log_return": thr,
        **_rank_metrics(valid_y, pred, thr),
    }

    out_arg = (args.out or "").strip()
    cfg_out = (get_config_value("EVENT_REACTION_CATBOOST_MODEL_PATH", "") or "").strip()
    if out_arg:
        out_final = out_arg
    elif cfg_out:
        out_final = cfg_out
    else:
        out_final = (
            "/app/logs/ml/models/event_reaction_forward5d_catboost.cbm"
            if Path("/app/logs").exists()
            else str(project_root / "local/models/event_reaction_forward5d_catboost.cbm")
        )

    metrics_blob: Dict[str, Any] = {
        "script": "train_event_reaction_catboost",
        "status": "ok",
        "dry_run": bool(args.dry_run),
        "model_version": MODEL_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": int(n_train),
        "n_valid": int(n_valid),
        "n_total": int(n_total),
        "dataset_version": args.dataset_version,
        "feature_builder_version": fbv,
        "min_rows_config": int(min_rows_eff),
        "min_rows_required": int(min_required),
        "metrics": {k: (round(v, 8) if isinstance(v, float) and math.isfinite(v) else v) for k, v in metrics.items()},
        "out_model_path": out_final,
    }
    _write_metrics_json(args.json_metrics_out, metrics_blob)
    logger.info(
        "Train=%s Valid=%s RMSE=%.5f MAE=%.5f",
        n_train,
        n_valid,
        metrics["rmse_valid"],
        metrics["mae_valid"],
    )

    if args.dry_run:
        logger.info("Dry-run: модель не записываем.")
        return 0

    out_path = Path(out_final)
    meta_path = out_path.with_suffix(".meta.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))
    meta = {
        "model_version": MODEL_VERSION,
        "feature_names": feature_names,
        "cat_feature_indices": cat_features,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": n_train,
        "n_valid": n_valid,
        "n_total": n_total,
        "target": "forward_log_ret_5d",
        "dataset_version": args.dataset_version,
        "feature_builder_version": fbv,
        "metrics": {k: (round(v, 6) if isinstance(v, float) and math.isfinite(v) else v) for k, v in metrics.items()},
        "note": "MVP advisory; join event_reaction_dataset for inference context.",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info("Сохранено: %s и %s", out_path, meta_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

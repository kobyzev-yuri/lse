#!/usr/bin/env python3
"""
Train a daily CatBoostRegressor for portfolio-game expected forward return.

The dataset uses all portfolio + 5m/correlation tickers with daily quotes.
The model output is advisory and is not used for automatic execution.

Examples:
  python scripts/train_portfolio_catboost.py --dry-run
  python scripts/train_portfolio_catboost.py --dry-run --json-metrics-out local/logs/ml_data_quality/portfolio_metrics.json
  python scripts/train_portfolio_catboost.py --horizon-days 5 --min-rows 300
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.sqrt(np.mean((y_pred - y_true) ** 2)))


def _mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.mean(np.abs(y_pred - y_true)))


def _rank_metrics(y_true: np.ndarray, y_pred: np.ndarray, threshold_log: float) -> dict:
    if y_true.size == 0:
        return {}
    n_top = max(1, int(math.ceil(y_true.size * 0.1)))
    order = np.argsort(y_pred)[::-1]
    top = y_true[order[:n_top]]
    all_hit = float(np.mean(y_true > threshold_log))
    top_hit = float(np.mean(top > threshold_log))
    # Spearman / Pearson on valid for separation quality (not just RMSE).
    spearman = float("nan")
    pearson = float("nan")
    pred_std = float(np.std(y_pred)) if y_pred.size else float("nan")
    try:
        if y_true.size >= 5 and np.std(y_true) > 1e-12 and np.std(y_pred) > 1e-12:
            from scipy.stats import spearmanr, pearsonr

            spearman = float(spearmanr(y_true, y_pred).correlation)
            pearson = float(pearsonr(y_true, y_pred)[0])
    except Exception:
        pass
    return {
        "top_decile_n": int(n_top),
        "top_decile_mean_log_return": float(np.mean(top)),
        "top_decile_mean_simple_pct": float((np.exp(np.mean(top)) - 1.0) * 100.0),
        "top_decile_hit_rate_pct": round(top_hit * 100.0, 2),
        "all_valid_hit_rate_pct": round(all_hit * 100.0, 2),
        "spearman_ic_valid": round(spearman, 6) if math.isfinite(spearman) else None,
        "pearson_ic_valid": round(pearson, 6) if math.isfinite(pearson) else None,
        "pred_std_valid": round(pred_std, 8) if math.isfinite(pred_std) else None,
        "top_minus_mean_log": float(np.mean(top) - np.mean(y_true)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Train portfolio daily expected-return CatBoost")
    parser.add_argument("--horizon-days", type=int, default=5, help="Forward return horizon in trading days")
    parser.add_argument("--corr-window-days", type=int, default=30, help="Rolling correlation window")
    parser.add_argument("--days", type=int, default=0, help="Limit quote history in calendar days (0 = all)")
    parser.add_argument("--min-rows", type=int, default=200, help="Minimum rows required to write a model")
    parser.add_argument("--valid-ratio", type=float, default=0.2, help="Last fraction of rows by date for validation")
    parser.add_argument("--dry-run", action="store_true", help="Build dataset and train, but do not write model")
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output .cbm path (meta written alongside as .meta.json)",
    )
    parser.add_argument(
        "--drop-price-level",
        action="store_true",
        help="Drop close/log_close features (recommended for horizon>=20 to improve cross-ticker separation)",
    )
    parser.add_argument(
        "--json-metrics-out",
        type=str,
        default="",
        help="Путь JSON с метриками/статусом (run_ml_data_quality_report, ml_train_readiness)",
    )
    args = parser.parse_args()

    def _write_metrics_json(path_str: str, payload: dict[str, Any]) -> None:
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

    from services.portfolio_ml_features import (
        MODEL_VERSION,
        build_portfolio_ml_dataset,
        feature_frame_to_rows,
        get_portfolio_ml_feature_schema,
        get_portfolio_ml_universe,
        portfolio_ml_threshold_log,
    )
    from config_loader import get_config_value

    out_arg = (args.out or "").strip()
    cfg_out = (get_config_value("PORTFOLIO_CATBOOST_MODEL_PATH", "") or "").strip()
    if out_arg:
        out_final = out_arg
    elif cfg_out:
        out_final = cfg_out
    else:
        out_final = (
            "/app/logs/ml/models/portfolio_return_catboost.cbm"
            if Path("/app/logs").exists()
            else str(project_root / "local" / "models" / "portfolio_return_catboost.cbm")
        )

    report_path_raw = (get_config_value("PORTFOLIO_ML_REPORT_JSONL", "") or "").strip()
    report_path = (
        Path(report_path_raw)
        if report_path_raw
        else (Path("/app/logs/ml/logs/portfolio_daily_ml_report.jsonl") if Path("/app/logs").exists() else (project_root / "local" / "logs" / "portfolio_daily_ml_report.jsonl"))
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)

    df = build_portfolio_ml_dataset(
        horizon_days=args.horizon_days,
        corr_window_days=args.corr_window_days,
        days=args.days if args.days and args.days > 0 else None,
        include_targets=True,
    )
    target_col = "target_log_return"
    if df.empty or target_col not in df.columns:
        logger.error("Нет строк датасета или target_log_return. Проверьте quotes.")
        _write_metrics_json(
            args.json_metrics_out,
            {
                "script": "train_portfolio_catboost",
                "status": "no_dataset",
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "note": "empty_or_no_target_log_return",
            },
        )
        return 2
    df = df.dropna(subset=[target_col]).copy()
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)
    n_total = len(df)
    universe = get_portfolio_ml_universe()
    logger.info(
        "Dataset rows=%s tickers=%s portfolio=%s game5m=%s leaders=%s horizon=%sd",
        n_total,
        df["ticker"].nunique(),
        len(universe.portfolio_tickers),
        len(universe.game5m_tickers),
        len(universe.leaders),
        args.horizon_days,
    )
    if n_total < max(20, int(args.min_rows)):
        logger.warning("Строк меньше порога %s — модель не пишем.", args.min_rows)
        _write_metrics_json(
            args.json_metrics_out,
            {
                "script": "train_portfolio_catboost",
                "status": "insufficient_rows",
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "n_total": int(n_total),
                "min_rows_config": int(args.min_rows),
                "horizon_days": int(args.horizon_days),
                "corr_window_days": int(args.corr_window_days),
            },
        )
        return 2

    drop_price = bool(args.drop_price_level) or int(args.horizon_days) >= 20
    model_version = MODEL_VERSION
    if drop_price:
        model_version = f"{MODEL_VERSION}_noprice"
    feature_names, cat_features, _ = get_portfolio_ml_feature_schema(drop_price_level=drop_price)
    colnames, cat_idx, rows = feature_frame_to_rows(df, feature_names=feature_names)
    if colnames != feature_names or cat_idx != cat_features:
        logger.error("Feature schema mismatch inside training script.")
        return 3
    if drop_price:
        logger.info("Using drop_price_level feature set (n_features=%s)", len(feature_names))
    y = df[target_col].astype(float).to_numpy()

    n_valid = max(1, int(n_total * float(args.valid_ratio)))
    n_train = n_total - n_valid
    if n_train < 20:
        n_train = max(20, n_total // 2)
        n_valid = n_total - n_train
    train_X, valid_X = rows[:n_train], rows[n_train:]
    train_y, valid_y = y[:n_train], y[n_train:]

    train_pool = Pool(train_X, label=train_y, cat_features=cat_features, feature_names=feature_names)
    valid_pool = Pool(valid_X, label=valid_y, cat_features=cat_features, feature_names=feature_names)

    # 20d: slightly deeper + lower L2 to allow more cross-sectional discrimination.
    if int(args.horizon_days) >= 20:
        model = CatBoostRegressor(
            iterations=800,
            learning_rate=0.03,
            depth=7,
            l2_leaf_reg=2.0,
            loss_function="RMSE",
            eval_metric="RMSE",
            random_seed=42,
            verbose=False,
            early_stopping_rounds=80,
        )
    else:
        model = CatBoostRegressor(
            iterations=500,
            learning_rate=0.04,
            depth=6,
            loss_function="RMSE",
            eval_metric="RMSE",
            random_seed=42,
            verbose=False,
            early_stopping_rounds=60,
        )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
    pred = np.asarray(model.predict(valid_pool), dtype=float)
    threshold_log = portfolio_ml_threshold_log()
    metrics = {
        "rmse_valid": _rmse(valid_y, pred),
        "mae_valid": _mae(valid_y, pred),
        "mean_target_valid_log_return": float(np.mean(valid_y)) if valid_y.size else None,
        "threshold_log_return": threshold_log,
        "drop_price_level": drop_price,
        **_rank_metrics(valid_y, pred, threshold_log),
    }
    logger.info(
        "Train=%s Valid=%s RMSE=%.5f MAE=%.5f top_decile_mean=%.2f%% hit=%.1f%% spearman=%.3f pred_std=%.4f",
        n_train,
        n_valid,
        metrics["rmse_valid"],
        metrics["mae_valid"],
        metrics.get("top_decile_mean_simple_pct", float("nan")),
        metrics.get("top_decile_hit_rate_pct", float("nan")),
        metrics.get("spearman_ic_valid") or float("nan"),
        metrics.get("pred_std_valid") or float("nan"),
    )

    metrics_blob = {
        "script": "train_portfolio_catboost",
        "status": "ok",
        "dry_run": bool(args.dry_run),
        "model_version": model_version,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": n_train,
        "n_valid": n_valid,
        "n_total": n_total,
        "tickers_n": int(df["ticker"].nunique()),
        "horizon_days": int(args.horizon_days),
        "corr_window_days": int(args.corr_window_days),
        "min_rows_config": int(args.min_rows),
        "drop_price_level": drop_price,
        "metrics": {k: (round(v, 8) if isinstance(v, float) and math.isfinite(v) else v) for k, v in metrics.items()},
        "out_model_path": out_final,
        "portfolio_tickers": universe.portfolio_tickers,
        "game5m_tickers": universe.game5m_tickers,
        "leader_tickers": universe.leaders,
    }
    _write_metrics_json(args.json_metrics_out, metrics_blob)

    if args.dry_run:
        logger.info("Dry-run: модель не записываем.")
        record = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "dry_run": True,
            "dataset_rows": int(n_total),
            "tickers": int(df["ticker"].nunique()),
            "horizon_days": int(args.horizon_days),
            "corr_window_days": int(args.corr_window_days),
            "min_rows": int(args.min_rows),
            "metrics": {k: (round(v, 8) if isinstance(v, float) and math.isfinite(v) else v) for k, v in metrics.items()},
            "out_model_path": out_final,
            "note": "dry_run_no_model_written",
        }
        with report_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("report appended: %s", report_path)
        return 0

    out_path = Path(out_final)
    meta_path = out_path.with_suffix(".meta.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))

    meta = {
        "model_version": model_version,
        "feature_names": feature_names,
        "cat_feature_indices": cat_features,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": n_train,
        "n_valid": n_valid,
        "n_total": n_total,
        "horizon_days": int(args.horizon_days),
        "corr_window_days": int(args.corr_window_days),
        "drop_price_level": drop_price,
        "target": "forward_log_return",
        "threshold_log_return": threshold_log,
        "metrics": {k: (round(v, 6) if isinstance(v, float) and math.isfinite(v) else v) for k, v in metrics.items()},
        "portfolio_tickers": universe.portfolio_tickers,
        "game5m_tickers": universe.game5m_tickers,
        "leader_tickers": universe.leaders,
        "note": "Daily advisory model for portfolio game; 20d drops close/log_close for cross-ticker separation.",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info("Сохранено: %s и %s", out_path, meta_path)

    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": False,
        "dataset_rows": int(n_total),
        "tickers": int(df["ticker"].nunique()),
        "horizon_days": int(args.horizon_days),
        "corr_window_days": int(args.corr_window_days),
        "min_rows": int(args.min_rows),
        "n_train": int(n_train),
        "n_valid": int(n_valid),
        "metrics": meta.get("metrics"),
        "out_model_path": str(out_path),
        "out_meta_path": str(meta_path),
    }
    with report_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("report appended: %s", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

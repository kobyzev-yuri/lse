"""
Advisory CatBoost signal for portfolio-game daily expected return.

Training writes local/models/portfolio_return_catboost.cbm and .meta.json.
If the package/model/config is missing, callers receive a status payload instead
of an exception.
"""

from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config_loader import get_config_value
from services.portfolio_ml_features import (
    DEFAULT_CORR_WINDOW_DAYS,
    build_latest_portfolio_ml_features,
    feature_frame_to_rows,
    get_portfolio_ml_feature_schema,
    portfolio_ml_threshold_log,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _load_model_bundle(model_path: str, model_mtime: float) -> Tuple[Any, Dict[str, Any]]:
    from catboost import CatBoostRegressor

    p = Path(model_path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    meta_path = p.with_suffix(".meta.json")
    if not meta_path.is_file():
        raise FileNotFoundError(str(meta_path))
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    model = CatBoostRegressor()
    model.load_model(str(p))
    return model, meta


def _default_model_path() -> str:
    root = Path(__file__).resolve().parents[1]
    return str(root / "local" / "models" / "portfolio_return_catboost.cbm")


def _runtime_guards() -> Tuple[str, str, Optional[str], Optional[Tuple[Any, Dict[str, Any]]]]:
    raw = (get_config_value("PORTFOLIO_CATBOOST_ENABLED", "false") or "false").strip().lower()
    if raw not in ("1", "true", "yes"):
        return "disabled", "Portfolio CatBoost выключен (PORTFOLIO_CATBOOST_ENABLED).", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "Пакет catboost не установлен (pip install -r requirements-catboost.txt).", None, None

    model_path = (get_config_value("PORTFOLIO_CATBOOST_MODEL_PATH", "") or "").strip() or _default_model_path()
    if not os.path.isfile(model_path):
        return "no_model_file", f"Нет файла модели: {model_path}", model_path, None
    try:
        mtime = os.path.getmtime(model_path)
    except OSError:
        mtime = 0.0
    try:
        bundle = _load_model_bundle(model_path, mtime)
    except Exception as e:
        logger.warning("Portfolio CatBoost load %s: %s", model_path, e)
        return "load_error", f"Ошибка загрузки модели: {e}", model_path, None
    return "ready", "", model_path, bundle


def _score_from_expected_log_return(value: Optional[float], threshold_log: float) -> Optional[float]:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    # 50 = roughly threshold; +5% log excess saturates near 100, -5% near 0.
    score = 50.0 + ((x - threshold_log) / 0.05) * 50.0
    return round(max(0.0, min(100.0, score)), 1)


def predict_portfolio_expected_returns(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Batch prediction for portfolio cards.

    Returns dict[ticker] with ml_* fields. Missing tickers receive no row.
    """
    status, note, model_path, bundle = _runtime_guards()
    base = {
        "portfolio_ml_status": status,
        "portfolio_ml_note": note,
        "portfolio_ml_model_path": model_path,
    }
    wanted = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    if status != "ready" or bundle is None:
        return {t: dict(base) for t in wanted}

    model, meta = bundle
    try:
        corr_window = int(meta.get("corr_window_days") or DEFAULT_CORR_WINDOW_DAYS)
    except (TypeError, ValueError):
        corr_window = DEFAULT_CORR_WINDOW_DAYS
    try:
        df = build_latest_portfolio_ml_features(wanted, corr_window_days=corr_window)
    except Exception as e:
        logger.warning("Portfolio CatBoost features: %s", e)
        err = {**base, "portfolio_ml_status": "feature_error", "portfolio_ml_note": str(e)}
        return {t: dict(err) for t in wanted}
    if df.empty:
        empty = {**base, "portfolio_ml_status": "no_features", "portfolio_ml_note": "Нет daily feature rows."}
        return {t: dict(empty) for t in wanted}

    colnames, cat_idx, rows = feature_frame_to_rows(df)
    expected = meta.get("feature_names")
    if list(expected or []) != colnames:
        msg = "Список признаков не совпадает с meta.json — переобучите portfolio CatBoost."
        logger.warning("Portfolio CatBoost feature mismatch meta=%s current=%s", expected, colnames)
        mismatch = {**base, "portfolio_ml_status": "feature_mismatch", "portfolio_ml_note": msg}
        return {t: dict(mismatch) for t in wanted}
    try:
        from catboost import Pool

        pool = Pool(rows, cat_features=cat_idx)
        preds = model.predict(pool)
    except Exception as e:
        logger.warning("Portfolio CatBoost predict: %s", e)
        err = {**base, "portfolio_ml_status": "predict_error", "portfolio_ml_note": str(e)}
        return {t: dict(err) for t in wanted}

    try:
        horizon = int(meta.get("horizon_days") or 5)
    except (TypeError, ValueError):
        horizon = 5
    threshold_log = float(meta.get("threshold_log_return") or portfolio_ml_threshold_log())
    out: Dict[str, Dict[str, Any]] = {}
    for i, (_, r) in enumerate(df.iterrows()):
        t = str(r.get("ticker") or "").strip().upper()
        try:
            pred_log = float(preds[i])
        except (TypeError, ValueError, IndexError):
            pred_log = float("nan")
        if not math.isfinite(pred_log):
            out[t] = {**base, "portfolio_ml_status": "bad_prediction", "portfolio_ml_note": "Модель вернула NaN."}
            continue
        pred_pct = (math.exp(pred_log) - 1.0) * 100.0
        out[t] = {
            **base,
            "portfolio_ml_status": "ok",
            "portfolio_ml_note": "",
            "portfolio_ml_horizon_days": horizon,
            "portfolio_ml_expected_log_return": round(pred_log, 6),
            "portfolio_ml_expected_return_pct": round(pred_pct, 2),
            "portfolio_ml_entry_score": _score_from_expected_log_return(pred_log, threshold_log),
            "portfolio_ml_threshold_pct": round((math.exp(threshold_log) - 1.0) * 100.0, 2),
            "portfolio_ml_cluster_role": str(r.get("cluster_role") or "unassigned"),
        }
    for t in wanted:
        out.setdefault(t, {**base, "portfolio_ml_status": "no_features", "portfolio_ml_note": "Нет feature row для тикера."})
    return out


def predict_portfolio_expected_return(ticker: str) -> Dict[str, Any]:
    t = str(ticker or "").strip().upper()
    return predict_portfolio_expected_returns([t]).get(t, {})

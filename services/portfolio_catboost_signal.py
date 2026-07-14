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


def _default_model_path(*, horizon_days: int = 5) -> str:
    root = Path(__file__).resolve().parents[1]
    if int(horizon_days) == 20:
        if Path("/app/logs").exists():
            return "/app/logs/ml/models/portfolio_return_catboost_20d.cbm"
        return str(root / "local" / "models" / "portfolio_return_catboost_20d.cbm")
    return str(root / "local" / "models" / "portfolio_return_catboost.cbm")


def _runtime_guards(
    *,
    horizon_days: int = 5,
) -> Tuple[str, str, Optional[str], Optional[Tuple[Any, Dict[str, Any]]]]:
    if int(horizon_days) == 20:
        enabled_key = "PORTFOLIO_CATBOOST_20D_ENABLED"
        path_key = "PORTFOLIO_CATBOOST_20D_MODEL_PATH"
        # Default on: shadow log_only on BUY/cards when .cbm exists.
        default_enabled = "true"
    else:
        enabled_key = "PORTFOLIO_CATBOOST_ENABLED"
        path_key = "PORTFOLIO_CATBOOST_MODEL_PATH"
        default_enabled = "false"

    raw = (get_config_value(enabled_key, default_enabled) or default_enabled).strip().lower()
    if raw not in ("1", "true", "yes"):
        return "disabled", f"Portfolio CatBoost выключен ({enabled_key}).", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "Пакет catboost не установлен (pip install -r requirements-catboost.txt).", None, None

    model_path = (get_config_value(path_key, "") or "").strip() or _default_model_path(horizon_days=horizon_days)
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


def _score_from_expected_log_return(
    value: Optional[float],
    threshold_log: float,
    *,
    scale: float = 0.05,
) -> Optional[float]:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    sc = max(1e-6, float(scale))
    # 50 = roughly threshold; +scale log excess saturates near 100.
    score = 50.0 + ((x - threshold_log) / sc) * 50.0
    return round(max(0.0, min(100.0, score)), 1)


def _field_prefix(horizon_days: int) -> str:
    return "portfolio_ml_20d" if int(horizon_days) == 20 else "portfolio_ml"


def predict_portfolio_expected_returns(
    tickers: List[str],
    *,
    horizon_days: int = 5,
) -> Dict[str, Dict[str, Any]]:
    """
    Batch prediction for portfolio cards / BUY context.

    horizon_days=5 → portfolio_ml_* fields (take/entry).
    horizon_days=20 → portfolio_ml_20d_* fields (log_only trend overlay).
    """
    prefix = _field_prefix(horizon_days)
    status, note, model_path, bundle = _runtime_guards(horizon_days=horizon_days)
    base = {
        f"{prefix}_status": status,
        f"{prefix}_note": note,
        f"{prefix}_model_path": model_path,
        f"{prefix}_horizon_days": int(horizon_days),
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
        logger.warning("Portfolio CatBoost features h=%s: %s", horizon_days, e)
        err = {**base, f"{prefix}_status": "feature_error", f"{prefix}_note": str(e)}
        return {t: dict(err) for t in wanted}
    if df.empty:
        empty = {
            **base,
            f"{prefix}_status": "no_features",
            f"{prefix}_note": "Нет daily feature rows.",
        }
        return {t: dict(empty) for t in wanted}

    colnames, cat_idx, rows = feature_frame_to_rows(df)
    expected = meta.get("feature_names")
    if list(expected or []) != colnames:
        msg = "Список признаков не совпадает с meta.json — переобучите portfolio CatBoost."
        logger.warning(
            "Portfolio CatBoost feature mismatch h=%s meta=%s current=%s",
            horizon_days,
            expected,
            colnames,
        )
        mismatch = {**base, f"{prefix}_status": "feature_mismatch", f"{prefix}_note": msg}
        return {t: dict(mismatch) for t in wanted}
    try:
        from catboost import Pool

        pool = Pool(rows, cat_features=cat_idx)
        preds = model.predict(pool)
    except Exception as e:
        logger.warning("Portfolio CatBoost predict h=%s: %s", horizon_days, e)
        err = {**base, f"{prefix}_status": "predict_error", f"{prefix}_note": str(e)}
        return {t: dict(err) for t in wanted}

    try:
        horizon = int(meta.get("horizon_days") or horizon_days)
    except (TypeError, ValueError):
        horizon = int(horizon_days)
    threshold_log = float(meta.get("threshold_log_return") or portfolio_ml_threshold_log())
    try:
        score_scale = float(
            (
                get_config_value(
                    "PORTFOLIO_CATBOOST_20D_SCORE_SCALE_LOG",
                    "0.12",
                )
                if int(horizon_days) == 20
                else "0.05"
            )
            or ("0.12" if int(horizon_days) == 20 else "0.05")
        )
    except (TypeError, ValueError):
        score_scale = 0.12 if int(horizon_days) == 20 else 0.05

    out: Dict[str, Dict[str, Any]] = {}
    for i, (_, r) in enumerate(df.iterrows()):
        t = str(r.get("ticker") or "").strip().upper()
        try:
            pred_log = float(preds[i])
        except (TypeError, ValueError, IndexError):
            pred_log = float("nan")
        if not math.isfinite(pred_log):
            out[t] = {
                **base,
                f"{prefix}_status": "bad_prediction",
                f"{prefix}_note": "Модель вернула NaN.",
            }
            continue
        pred_pct = (math.exp(pred_log) - 1.0) * 100.0
        out[t] = {
            **base,
            f"{prefix}_status": "ok",
            f"{prefix}_note": "",
            f"{prefix}_horizon_days": horizon,
            f"{prefix}_expected_log_return": round(pred_log, 6),
            f"{prefix}_expected_return_pct": round(pred_pct, 2),
            f"{prefix}_entry_score": _score_from_expected_log_return(
                pred_log, threshold_log, scale=score_scale
            ),
            f"{prefix}_threshold_pct": round((math.exp(threshold_log) - 1.0) * 100.0, 2),
            f"{prefix}_cluster_role": str(r.get("cluster_role") or "unassigned"),
            f"{prefix}_gate_mode": "log_only",
        }
    for t in wanted:
        out.setdefault(
            t,
            {
                **base,
                f"{prefix}_status": "no_features",
                f"{prefix}_note": "Нет feature row для тикера.",
            },
        )
    return out


def predict_portfolio_expected_return(ticker: str) -> Dict[str, Any]:
    t = str(ticker or "").strip().upper()
    return predict_portfolio_expected_returns([t]).get(t, {})


def predict_portfolio_expected_returns_20d(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    return predict_portfolio_expected_returns(tickers, horizon_days=20)


def predict_portfolio_expected_return_20d(ticker: str) -> Dict[str, Any]:
    t = str(ticker or "").strip().upper()
    return predict_portfolio_expected_returns_20d([t]).get(t, {})


def portfolio_ml_20d_regime_hint(score: Optional[float], rule_regime: Optional[str]) -> str:
    """Soft fusion note: CatBoost 20d vs rule regime (log_only, no apply)."""
    reg = (rule_regime or "neutral").strip().lower()
    try:
        sc = float(score) if score is not None else None
    except (TypeError, ValueError):
        sc = None
    if sc is None:
        return "no_score"
    if sc >= 62 and reg in ("melt_up", "trend_up"):
        return "align_uptrend"
    if sc >= 62 and reg == "breakdown":
        return "conflict_long_in_breakdown"
    if sc <= 40 and reg in ("melt_up", "trend_up"):
        return "conflict_weak_ml_vs_uptrend"
    if sc <= 40 and reg == "breakdown":
        return "align_breakdown"
    return "neutral"

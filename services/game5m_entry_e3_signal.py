"""Shadow CatBoost entry E3 (full T+N+C bar features) for decision_stack telemetry."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.game5m_entry_bar_dataset import row_from_bar_dataset_dict

logger = logging.getLogger(__name__)


def _default_e3_model_path() -> str:
    from config_loader import get_config_value

    model_path = (get_config_value("GAME_5M_ENTRY_E3_MODEL_PATH", "") or "").strip()
    if model_path:
        return model_path
    if Path("/app/logs").exists():
        return "/app/logs/ml/models/game5m_entry_catboost_e3.cbm"
    return str(Path(__file__).resolve().parents[1] / "local" / "models" / "game5m_entry_catboost_e3.cbm")


def _e3_log_enabled() -> bool:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_ENTRY_E3_LOG_ENABLED", "true") or "true").strip().lower()
    return raw in ("1", "true", "yes")


def _price_to_low5d_ratio(d5: Dict[str, Any]) -> float:
    from services.catboost_5m_signal import _safe_float

    high_5d = _safe_float(d5.get("high_5d"), 0.0)
    low_5d = _safe_float(d5.get("low_5d"), 0.0)
    price = _safe_float(d5.get("price"), 0.0)
    if high_5d > low_5d and price > 0:
        return (price - low_5d) / (high_5d - low_5d)
    return 0.5


def build_entry_e3_feature_row(ticker: str, d5: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
    """Full bar row: tech from d5 + NC from build_entry_context_features (no leak)."""
    from services.game5m_ml_context_features import build_entry_context_features

    sym = str(ticker or "").strip().upper()
    ms = d5.get("market_session") or {}
    bar_ts = (
        d5.get("decision_5m_bar_open_et")
        or d5.get("bar_ts_et")
        or ms.get("now_et")
        or ""
    )
    row_dict: Dict[str, Any] = {
        "ticker": sym,
        "rsi_5m": d5.get("rsi_5m"),
        "momentum_2h_pct": d5.get("momentum_2h_pct"),
        "momentum_rth_today_pct": d5.get("momentum_rth_today_pct"),
        "volatility_5m_pct": d5.get("volatility_5m_pct"),
        "pullback_from_high_pct": d5.get("pullback_from_high_pct"),
        "bars_count": d5.get("bars_count"),
        "momentum_rth_today_bars": d5.get("momentum_rth_today_bars"),
        "price_to_low5d_ratio": _price_to_low5d_ratio(d5),
        "prob_up": d5.get("prob_up"),
        "prob_down": d5.get("prob_down"),
        "macro_risk_level": d5.get("macro_risk_level"),
        "ndx_gap_pct": d5.get("ndx_gap_pct"),
        "spy_gap_pct": d5.get("spy_gap_pct"),
        "premarket_gap_pct": d5.get("premarket_gap_pct"),
    }
    if bar_ts:
        try:
            ctx = build_entry_context_features(
                ticker=sym,
                bar_ts_et=str(bar_ts),
                features=row_dict,
                entry_context=d5,
            )
            row_dict.update(ctx)
        except Exception as e:
            logger.debug("entry_e3 context enrich %s: %s", sym, e)
    from services.game5m_entry_bar_dataset import get_bar_train_feature_schema

    colnames, _ = get_bar_train_feature_schema("full")
    return colnames, row_from_bar_dataset_dict(row_dict, sym, mode="full")


def _runtime_guards() -> Tuple[str, str, Optional[str], Optional[Tuple[Any, Dict[str, Any]]]]:
    if not _e3_log_enabled():
        return "disabled", "Entry E3 shadow выключен (GAME_5M_ENTRY_E3_LOG_ENABLED).", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "Пакет catboost не установлен.", None, None
    model_path = _default_e3_model_path()
    if not os.path.isfile(model_path):
        return "no_model_file", f"Нет E3 модели: {model_path}", model_path, None
    try:
        from services.catboost_5m_signal import _load_model_bundle

        mtime = os.path.getmtime(model_path)
        bundle = _load_model_bundle(model_path, mtime)
    except Exception as e:
        logger.warning("entry E3 load %s: %s", model_path, e)
        return "load_error", str(e), model_path, None
    return "ready", "", model_path, bundle


def attach_entry_e3_signal(out: Dict[str, Any], ticker: str) -> None:
    """Shadow telemetry: P(y_entry_good) from E3 CatBoost; never mutates decision."""
    from services.catboost_5m_signal import _catboost_predict_proba_row

    out.setdefault("entry_e3_signal_status", "skipped")
    out.setdefault("entry_e3_signal_note", "")
    out.setdefault("catboost_entry_proba_good_e3", None)

    st_g, note_g, _mp, bundle = _runtime_guards()
    if st_g != "ready" or bundle is None:
        out["entry_e3_signal_status"] = st_g
        out["entry_e3_signal_note"] = note_g
        return

    model, meta = bundle
    try:
        colnames, row = build_entry_e3_feature_row(ticker, out)
        p_st, p_good, p_note = _catboost_predict_proba_row(model, meta, colnames, row)
        out["entry_e3_signal_status"] = p_st
        out["catboost_entry_proba_good_e3"] = p_good
        if p_st != "ok":
            out["entry_e3_signal_note"] = p_note
            return
        out["entry_e3_signal_note"] = (
            f"CatBoost entry E3 (shadow): P(y_entry_good)≈{p_good:.2f} — log_only."
        )
    except Exception as e:
        logger.warning("entry E3 predict: %s", e)
        out["entry_e3_signal_status"] = "predict_error"
        out["entry_e3_signal_note"] = str(e)


__all__ = [
    "attach_entry_e3_signal",
    "build_entry_e3_feature_row",
]

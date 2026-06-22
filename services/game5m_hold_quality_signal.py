"""Shadow hold-bar CatBoost H3 (y_hold_good) for exit telemetry."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


def default_hold_quality_model_path() -> Path:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_HOLD_QUALITY_MODEL_PATH", "") or "").strip()
    if raw:
        return Path(raw)
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/models/game5m_hold_bar_catboost_h3.cbm")
    return Path(__file__).resolve().parents[1] / "local" / "models" / "game5m_hold_bar_catboost_h3.cbm"


def _hold_quality_log_enabled() -> bool:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_HOLD_QUALITY_LOG_ENABLED", "true") or "true").strip().lower()
    return raw in ("1", "true", "yes")


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def row_vector_from_live_hold(
    *,
    ticker: str,
    entry_price: float,
    entry_ts_et: pd.Timestamp,
    bar_ts_et: pd.Timestamp,
    ref_close: float,
    entry_ctx: Optional[Dict[str, Any]],
    exit_features: Optional[Dict[str, Any]] = None,
) -> Optional[List[Any]]:
    """H3 feature row aligned with train_game5m_hold_bar_catboost.py full mode."""
    from services.game5m_hold_bar_dataset import row_from_hold_bar_dict
    from services.game5m_ml_context_features import (
        build_entry_context_features,
        entry_snapshot_from_context,
        hold_exit_tech_from_features,
        hold_state_features,
    )

    sym = str(ticker or "").strip().upper()
    if not sym or entry_price <= 0 or ref_close <= 0:
        return None
    state = hold_state_features(
        entry_price=float(entry_price),
        entry_ts_et=entry_ts_et,
        bar_ts_et=bar_ts_et,
        ref_close=float(ref_close),
    )
    feat = dict(exit_features or {})
    row_dict: Dict[str, Any] = {"ticker": sym}
    row_dict.update(state)
    row_dict.update(entry_snapshot_from_context(entry_ctx or {}))
    row_dict.update(hold_exit_tech_from_features(feat))
    try:
        ctx = build_entry_context_features(
            ticker=sym,
            bar_ts_et=bar_ts_et.isoformat(),
            features=feat,
            entry_context=entry_ctx or {},
        )
        row_dict.update(ctx)
    except Exception as e:
        logger.debug("hold_quality context %s: %s", sym, e)
    return row_from_hold_bar_dict(row_dict, sym, mode="full")


def predict_hold_quality_proba(
    model_path: str | Path,
    row: Sequence[Any],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "error", "hold_quality_proba": None}
    path = Path(model_path)
    if not path.is_file():
        out["status"] = "no_model_file"
        out["reason"] = str(path)
        return out
    try:
        from catboost import CatBoostClassifier, Pool
        from services.game5m_hold_bar_dataset import get_hold_bar_train_feature_schema
    except ImportError:
        out["status"] = "no_catboost"
        return out

    names, cats = get_hold_bar_train_feature_schema("full")
    if meta and isinstance(meta.get("feature_names"), list):
        names_m = [str(x) for x in meta["feature_names"]]
        if names_m != names:
            out["status"] = "feature_mismatch"
            return out
    if len(row) != len(names):
        out["status"] = "feature_mismatch"
        out["reason"] = f"row len {len(row)} != {len(names)}"
        return out
    try:
        model = CatBoostClassifier()
        model.load_model(str(path))
        pool = Pool([list(row)], cat_features=cats, feature_names=names)
        proba = model.predict_proba(pool)[0, 1]
        out["status"] = "ok"
        out["hold_quality_proba"] = float(proba)
    except Exception as exc:
        out["status"] = "predict_error"
        out["reason"] = str(exc)
    return out


def load_hold_quality_meta(model_path: str | Path) -> Optional[Dict[str, Any]]:
    p = Path(model_path).with_suffix(".meta.json")
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def build_hold_quality_shadow(
    *,
    ticker: str,
    open_position: Dict[str, Any],
    entry_ctx: Optional[Dict[str, Any]],
    ref_close: float,
    bar_time_et: str | None,
    exit_features: Optional[Dict[str, Any]] = None,
    exit_detail: str = "",
) -> Dict[str, Any]:
    """Telemetry dict for exit_context_json (log_only by default)."""
    from config_loader import get_config_value

    enabled = _hold_quality_log_enabled()
    base: Dict[str, Any] = {
        "enabled": enabled,
        "log_only": True,
        "contour": "hold_bar_ml_h3",
        "exit_detail": exit_detail or "",
        "status": "skipped",
    }
    if not enabled:
        base["skip_reason"] = "disabled"
        return base

    model_path = default_hold_quality_model_path()
    base["model_path"] = str(model_path)
    if not model_path.is_file():
        base["status"] = "no_model_file"
        return base

    entry = open_position.get("entry_price")
    entry_price = float(entry) if isinstance(entry, (int, float)) and entry > 0 else None
    if not entry_price:
        base["skip_reason"] = "missing_entry_price"
        return base

    try:
        from services.game_5m import TRADE_HISTORY_TZ, CHART_DISPLAY_TZ

        entry_ts = open_position.get("entry_ts")
        et = pd.Timestamp(entry_ts)
        if et.tzinfo is None:
            et = et.tz_localize(TRADE_HISTORY_TZ, ambiguous=True).tz_convert(CHART_DISPLAY_TZ)
        else:
            et = et.tz_convert(CHART_DISPLAY_TZ)
        bt = pd.Timestamp(bar_time_et) if bar_time_et else pd.Timestamp.now(tz=CHART_DISPLAY_TZ)
        if bt.tzinfo is None:
            bt = bt.tz_localize(CHART_DISPLAY_TZ, ambiguous=True)
        else:
            bt = bt.tz_convert(CHART_DISPLAY_TZ)
    except Exception:
        base["skip_reason"] = "timestamp_parse_error"
        return base

    row = row_vector_from_live_hold(
        ticker=ticker,
        entry_price=entry_price,
        entry_ts_et=et,
        bar_ts_et=bt,
        ref_close=float(ref_close),
        entry_ctx=entry_ctx,
        exit_features=exit_features,
    )
    if row is None:
        base["skip_reason"] = "feature_row_failed"
        return base

    meta = load_hold_quality_meta(model_path)
    pred = predict_hold_quality_proba(model_path, row, meta=meta)
    base["status"] = pred.get("status")
    base["hold_quality_proba"] = pred.get("hold_quality_proba")
    if pred.get("reason"):
        base["reason"] = pred.get("reason")

    try:
        tau = float((get_config_value("GAME_5M_HOLD_QUALITY_TAU_HOLD", "0.55") or "0.55").strip())
    except (TypeError, ValueError):
        tau = 0.55
    base["tau_hold"] = max(0.0, min(1.0, tau))
    p = base.get("hold_quality_proba")
    if base["status"] == "ok" and p is not None:
        base["would_defer_exit"] = float(p) >= tau
    return base


__all__ = [
    "build_hold_quality_shadow",
    "default_hold_quality_model_path",
    "predict_hold_quality_proba",
    "row_vector_from_live_hold",
]

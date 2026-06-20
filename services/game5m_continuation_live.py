"""Live continuation ML telemetry at TAKE (phase 2.4–2.6, log_only by default)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from services.decision_stack._types import READINESS_PRODUCTION, gate_mode, stack_readiness
from services.game5m_continuation_catboost import (
    default_continuation_catboost_model_path,
    load_continuation_model_meta,
    predict_continuation_missed_upside_proba,
    row_vector_from_trade_effect,
)
from services.game5m_continuation_dataset import continuation_promotion_auc_min
from services.multiday_lr_gate import should_block_continuation_take_defer


def _cfg_float(key: str, default: float) -> float:
    from config_loader import get_config_value

    raw = (get_config_value(key, str(default)) or str(default)).strip().replace(",", ".")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _cfg_int(key: str, default: int) -> int:
    from config_loader import get_config_value

    raw = (get_config_value(key, str(default)) or str(default)).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _continuation_gate_mode() -> str:
    from config_loader import get_config_value

    raw = (get_config_value("DECISION_STACK_CONTINUATION_ML_GATE_MODE", "") or "").strip().lower()
    if raw in ("none", "log_only", "apply"):
        return raw
    return gate_mode("GAME_5M_CONTINUATION_ML_GATE_MODE", "log_only")


def evaluate_continuation_ml_at_take(
    *,
    ticker: str,
    exit_signal: str,
    entry_price: float,
    exit_price: float,
    hold_minutes: float,
    d5: Optional[Dict[str, Any]] = None,
    entry_ctx: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    CatBoost continuation gate for TAKE_PROFIT / TAKE_PROFIT_SUSPEND.
    Returns telemetry dict for context_json key ``continuation_ml`` (D4a analog).
    """
    from config_loader import get_config_value

    raw_enabled = (get_config_value("GAME_5M_CONTINUATION_ML_ENABLED", "false") or "false").strip().lower()
    enabled = raw_enabled in ("1", "true", "yes")
    if not enabled:
        return None

    gm = _continuation_gate_mode()
    raw_log_only = (get_config_value("GAME_5M_CONTINUATION_ML_LOG_ONLY", "true") or "true").strip().lower()
    config_log_only = raw_log_only in ("1", "true", "yes")
    log_only = gm != "apply" or config_log_only

    readiness = stack_readiness("continuation_ml")
    apply_allowed = (not log_only) and readiness == READINESS_PRODUCTION and gm == "apply"

    tau = _cfg_float("GAME_5M_CONTINUATION_LIVE_TAU", _cfg_float("GAME_5M_CONTINUATION_SCENARIO_TAU", 0.55))
    tau = max(0.0, min(1.0, tau))
    defer_bars = max(1, min(48, _cfg_int("GAME_5M_CONTINUATION_SCENARIO_DELAY_BARS", 6)))

    d5d = d5 if isinstance(d5, dict) else {}
    ect = entry_ctx if isinstance(entry_ctx, dict) else {}

    realized_pct = None
    if entry_price > 0 and exit_price > 0:
        realized_pct = (float(exit_price) / float(entry_price) - 1.0) * 100.0

    take_pct = ect.get("take_profit_pct")
    if take_pct is None:
        take_pct = ect.get("entry_take_profit_pct")

    row = row_vector_from_trade_effect(
        ticker=ticker,
        exit_signal=exit_signal,
        realized_pct=float(realized_pct or 0.0),
        hold_minutes=float(hold_minutes),
        entry_rsi_5m=ect.get("rsi_5m"),
        entry_momentum_2h_pct=ect.get("momentum_2h_pct") or d5d.get("momentum_2h_pct"),
        entry_vol_5m_pct=ect.get("volatility_5m_pct") or d5d.get("volatility_5m_pct"),
        entry_prob_up=ect.get("prob_up") or d5d.get("prob_up"),
        entry_take_profit_pct=take_pct,
        exit_rsi_5m=d5d.get("rsi_5m"),
        exit_momentum_2h_pct=d5d.get("momentum_2h_pct"),
        exit_volatility_5m_pct=d5d.get("volatility_5m_pct"),
        trade_mfe_pct=d5d.get("pullback_from_high_pct"),
    )

    out: Dict[str, Any] = {
        "enabled": True,
        "log_only": log_only,
        "gate_mode": gm,
        "stack_readiness": readiness,
        "apply_allowed": apply_allowed,
        "tau": tau,
        "defer_bars": defer_bars,
        "promotion_auc_min": continuation_promotion_auc_min(),
        "exit_signal": str(exit_signal or "").strip().upper(),
        "would_defer_take": False,
        "would_defer_by_model": False,
        "multiday_block": False,
    }

    if row is None:
        out["status"] = "skipped"
        out["skip_reason"] = "feature_row_unavailable"
        return out

    model_path = (get_config_value("GAME_5M_CONTINUATION_CATBOOST_MODEL_PATH", "") or "").strip()
    if not model_path:
        model_path = str(default_continuation_catboost_model_path())
    out["model_path"] = model_path
    meta = load_continuation_model_meta(model_path)

    pred = predict_continuation_missed_upside_proba(model_path, row, meta=meta if isinstance(meta, dict) else None)
    out["predict_status"] = pred.get("status")
    if pred.get("status") != "ok":
        out["status"] = "predict_failed"
        out["skip_reason"] = pred.get("reason")
        return out

    proba = float(pred["continuation_proba"])
    out["status"] = "ok"
    out["continuation_proba"] = proba
    out["would_defer_by_model"] = proba > tau

    blocked, md_meta = should_block_continuation_take_defer(d5d, pnl_current_pct=realized_pct)
    out["multiday_hold"] = md_meta
    out["multiday_block"] = bool(blocked)

    would_defer = out["would_defer_by_model"] and not blocked
    out["would_defer_take"] = would_defer
    if blocked and out["would_defer_by_model"]:
        out["defer_block_reason"] = "multiday_hold_apply_bullish"
    return out


def continuation_ml_should_defer_take(gate: Optional[Dict[str, Any]]) -> bool:
    """Apply path (phase 2.6): defer TAKE only when ML gate allows and multiday did not block."""
    if not isinstance(gate, dict) or gate.get("status") != "ok":
        return False
    if gate.get("log_only") or not gate.get("apply_allowed"):
        gate["apply_skip_reason"] = "log_only_or_not_production_ready"
        return False
    if gate.get("multiday_block"):
        gate["apply_skip_reason"] = gate.get("defer_block_reason") or "multiday_block"
        return False
    if not gate.get("would_defer_take"):
        gate["apply_skip_reason"] = "proba_below_tau"
        return False
    gate["applied"] = True
    gate["defer_close"] = True
    gate["apply_reason"] = "continuation_ml_high_missed_upside_proba"
    return True

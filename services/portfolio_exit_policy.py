"""
Портфельная игра: эффективный тейк (стратегия + ML-снимок на входе) и trailing take по откату от пика.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, Optional, Tuple

from config_loader import get_config_value
from services.portfolio_trend_regime import regime_from_context

logger = logging.getLogger(__name__)


def _truthy(raw: str, default: str = "false") -> bool:
    return (get_config_value(raw, default) or default).strip().lower() in ("1", "true", "yes", "on")


def _float_cfg(key: str, default: float) -> float:
    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def clamp_take_pct(value: float, *, floor_pct: float, cap_pct: float) -> float:
    return max(floor_pct, min(cap_pct, value))


def ml_take_params() -> Tuple[bool, float, float, float]:
    """enabled, factor, floor, cap."""
    if not _truthy("PORTFOLIO_ML_TAKE_ENABLED", "true"):
        return False, 1.5, 4.0, 18.0
    return (
        True,
        _float_cfg("PORTFOLIO_ML_TAKE_FACTOR", 1.5),
        _float_cfg("PORTFOLIO_ML_TAKE_FLOOR_PCT", 4.0),
        _float_cfg("PORTFOLIO_ML_TAKE_CAP_PCT", 18.0),
    )


def ml_take_params_for_regime(regime: str | None = None) -> Tuple[bool, float, float, float]:
    enabled, factor, floor_p, cap_p = ml_take_params()
    r = (regime or "neutral").strip().lower()
    if r == "melt_up":
        cap_p = max(cap_p, _float_cfg("PORTFOLIO_TREND_MELT_UP_TAKE_CAP_PCT", 35.0))
    elif r == "breakdown":
        cap_p = min(cap_p, _float_cfg("PORTFOLIO_TREND_BREAKDOWN_TAKE_CAP_PCT", 12.0))
    return enabled, factor, floor_p, cap_p


def trailing_take_params() -> Tuple[bool, float, float]:
    """enabled, min_profit_to_arm, pullback_from_peak_pct."""
    if not _truthy("PORTFOLIO_TRAILING_TAKE_ENABLED", "true"):
        return False, 8.0, 3.0
    return (
        True,
        _float_cfg("PORTFOLIO_TRAILING_MIN_PROFIT_PCT", 8.0),
        _float_cfg("PORTFOLIO_TRAILING_PULLBACK_PCT", 3.0),
    )


def trailing_take_params_for_regime(regime: str | None = None) -> Tuple[bool, float, float]:
    enabled, min_arm, pullback = trailing_take_params()
    r = (regime or "neutral").strip().lower()
    if r == "melt_up":
        min_arm = _float_cfg("PORTFOLIO_TREND_MELT_UP_TRAILING_MIN_PROFIT_PCT", 14.0)
        pullback = _float_cfg("PORTFOLIO_TREND_MELT_UP_TRAILING_PULLBACK_PCT", 7.0)
    elif r == "breakdown":
        min_arm = _float_cfg("PORTFOLIO_TREND_BREAKDOWN_TRAILING_MIN_PROFIT_PCT", 5.0)
        pullback = _float_cfg("PORTFOLIO_TREND_BREAKDOWN_TRAILING_PULLBACK_PCT", 2.0)
    return enabled, min_arm, pullback


def _parse_context(context_json: Any) -> Dict[str, Any]:
    if context_json is None:
        return {}
    if isinstance(context_json, dict):
        return context_json
    if isinstance(context_json, str):
        try:
            data = json.loads(context_json)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def ml_expected_pct_from_context(ctx: Dict[str, Any]) -> Optional[float]:
    raw = ctx.get("portfolio_ml_expected_return_pct")
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def compute_ml_adjusted_take(
    base_take: float,
    ml_expected_pct: Optional[float],
    *,
    regime: str | None = None,
) -> Tuple[float, str]:
    """Поднять тейк по снимку ML на входе (не пересчитывать на выходе)."""
    enabled, factor, floor_p, cap_p = ml_take_params_for_regime(regime)
    if not enabled or base_take <= 0:
        return base_take, "base"
    if ml_expected_pct is None or ml_expected_pct <= 0:
        return clamp_take_pct(base_take, floor_pct=floor_p, cap_pct=cap_p), "base"
    dynamic = max(base_take, factor * ml_expected_pct)
    eff = clamp_take_pct(dynamic, floor_pct=floor_p, cap_pct=cap_p)
    return eff, f"ml_adj(base={base_take:.1f},exp={ml_expected_pct:.2f},factor={factor})"


def compute_entry_effective_take_for_ticker(
    ticker: str,
    base_take: Optional[float],
    context_json: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], str]:
    ctx = dict(context_json or {})
    if base_take is None or base_take <= 0:
        return None, "none"
    ml_pct = ml_expected_pct_from_context(ctx)
    if ml_pct is None:
        from services.portfolio_entry_guards import portfolio_ml_snapshot

        snap = portfolio_ml_snapshot(ticker)
        for k, v in snap.items():
            if k.startswith("portfolio_ml_"):
                ctx[k] = v
        ml_pct = ml_expected_pct_from_context(ctx)
    eff, note = compute_ml_adjusted_take(float(base_take), ml_pct, regime=regime_from_context(ctx))
    return eff, note


def peak_pnl_pct_since_entry(
    engine,
    ticker: str,
    entry_price: float,
    entry_ts,
) -> Optional[float]:
    """Пик нереализованного P/L % по daily high с даты входа."""
    if entry_price <= 0 or entry_ts is None:
        return None
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT MAX(high) FROM quotes
                    WHERE ticker = :ticker AND date >= CAST(:entry_ts AS date)
                    """
                ),
                {"ticker": ticker, "entry_ts": entry_ts},
            ).fetchone()
        if not row or row[0] is None:
            return None
        peak = float(row[0])
        if peak <= 0:
            return None
        return (peak - entry_price) / entry_price * 100.0
    except Exception as e:
        logger.debug("peak_pnl %s: %s", ticker, e)
        return None


def trailing_take_should_close(
    pnl_pct: float,
    peak_pnl_pct: Optional[float],
    *,
    regime: str | None = None,
) -> Tuple[bool, str]:
    enabled, min_arm, pullback = trailing_take_params_for_regime(regime)
    if not enabled or peak_pnl_pct is None:
        return False, ""
    if peak_pnl_pct < min_arm:
        return False, ""
    giveback = peak_pnl_pct - pnl_pct
    if giveback >= pullback:
        return True, (
            f"Trailing take: peak={peak_pnl_pct:.2f}%, pnl={pnl_pct:.2f}%, "
            f"giveback={giveback:.2f}% >= {pullback:.2f}%"
        )
    return False, ""


def resolve_effective_take_pct(
    ticker: str,
    buy_take_pct: Optional[float],
    *,
    context_json: Any = None,
    from_config_fallback: bool = False,
) -> Tuple[float, str]:
    """
    Итоговый порог тейка: BUY.take_profit → snapshot в context → ML adj → config fallback.
    """
    ctx = _parse_context(context_json)
    snap = ctx.get("portfolio_effective_take_pct_at_entry")
    if snap is not None:
        try:
            v = float(snap)
            if v > 0:
                return v, "entry_snapshot"
        except (TypeError, ValueError):
            pass

    base = buy_take_pct
    if base is None or base <= 0:
        try:
            base = float(get_config_value("PORTFOLIO_TAKE_PROFIT_PCT", "0").strip() or "0")
        except (ValueError, TypeError):
            base = 0.0
        from_config_fallback = True
    if base is None or base <= 0:
        return 0.0, "none"

    ml_pct = ml_expected_pct_from_context(ctx)
    eff, note = compute_ml_adjusted_take(float(base), ml_pct, regime=regime_from_context(ctx))
    if from_config_fallback:
        return eff, f"config_fallback;{note}"
    return eff, note


def evaluate_portfolio_exit(
    *,
    engine,
    ticker: str,
    entry_price: float,
    entry_ts,
    current_price: float,
    buy_take_pct: Optional[float],
    context_json: Any = None,
) -> Tuple[bool, str, str]:
    """
    Returns: (should_close, human_reason, signal_type)
    signal_type: TAKE_PROFIT | TRAILING_TAKE
    """
    ctx = _parse_context(context_json)
    regime = regime_from_context(ctx)
    pnl_pct = (current_price - entry_price) / entry_price * 100.0
    peak = peak_pnl_pct_since_entry(engine, ticker, entry_price, entry_ts)
    trail_close, trail_reason = trailing_take_should_close(pnl_pct, peak, regime=regime)
    if trail_close:
        return True, trail_reason, "TRAILING_TAKE"

    take_pct, take_note = resolve_effective_take_pct(
        ticker, buy_take_pct, context_json=context_json
    )
    if take_pct > 0 and pnl_pct >= take_pct - 0.05:
        return (
            True,
            f"Take-profit ({take_note}): pnl={pnl_pct:.2f}% >= {take_pct:.2f}%",
            "TAKE_PROFIT",
        )
    return False, "", ""

# -*- coding: utf-8 -*-
"""Observable premarket-gap baseline for GAME_5M decisions."""
from __future__ import annotations

from typing import Any, Dict, Optional


def _cfg_float(key: str, default: float) -> float:
    try:
        from config_loader import get_config_value

        return float((get_config_value(key, str(default)) or str(default)).strip())
    except Exception:
        return default


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate_premarket_gap_baseline(
    premarket_gap_pct: Any,
    *,
    very_negative_news: bool = False,
    macro_risk_level: Optional[str] = None,
    macro_equity_gap_bias: Optional[str] = None,
    multiday_horizon_1d_pct: Any = None,
) -> Optional[Dict[str, Any]]:
    """Return a conservative, observable baseline signal from premarket gap."""
    gap = _f(premarket_gap_pct)
    if gap is None:
        return None

    pos_min = _cfg_float("GAME_5M_PREMARKET_GAP_BASELINE_BULLISH_MIN_PCT", 1.0)
    neg_caution = _cfg_float("GAME_5M_PREMARKET_GAP_BASELINE_NEGATIVE_CAUTION_PCT", -2.0)
    chase_min = _cfg_float("GAME_5M_PREMARKET_GAP_BASELINE_CHASE_RISK_PCT", 4.0)
    bearish_md = _cfg_float("GAME_5M_PREMARKET_GAP_BASELINE_BEARISH_MULTIDAY_PCT", -0.15)

    macro_level = (macro_risk_level or "").strip().upper()
    macro_bias = (macro_equity_gap_bias or "").strip().upper()
    h1 = _f(multiday_horizon_1d_pct)
    blocked_by_context = bool(
        very_negative_news
        or macro_level == "AVOID"
        or macro_bias == "DOWN"
        or (h1 is not None and h1 <= bearish_md)
    )

    signal = "neutral"
    action = "telemetry"
    entry_advice = None
    reason = "premarket gap baseline neutral"
    should_boost = False
    should_caution = False

    if gap <= neg_caution:
        signal = "bearish_gap"
        action = "downgrade"
        entry_advice = "CAUTION"
        should_caution = True
        reason = f"premarket gap {gap:+.2f}% <= {neg_caution:+.2f}%"
    elif gap >= chase_min:
        signal = "chase_risk"
        action = "downgrade"
        entry_advice = "CAUTION"
        should_caution = True
        reason = f"premarket gap {gap:+.2f}% >= chase risk {chase_min:+.2f}%"
    elif gap >= pos_min:
        signal = "bullish_gap"
        if blocked_by_context:
            action = "telemetry"
            entry_advice = "CAUTION"
            should_caution = True
            reason = f"premarket gap {gap:+.2f}% bullish, but context blocks boost"
        else:
            action = "boost"
            entry_advice = "ALLOW"
            should_boost = True
            reason = f"premarket gap {gap:+.2f}% >= bullish baseline {pos_min:+.2f}%"

    return {
        "source": "premarket_gap_observable_baseline",
        "premarket_gap_pct": round(gap, 4),
        "signal": signal,
        "action": action,
        "entry_advice": entry_advice,
        "reason": reason,
        "should_boost_entry": should_boost,
        "should_caution_entry": should_caution,
        "blocked_by_context": blocked_by_context,
        "thresholds": {
            "bullish_min_pct": pos_min,
            "negative_caution_pct": neg_caution,
            "chase_risk_pct": chase_min,
            "bearish_multiday_pct": bearish_md,
        },
    }

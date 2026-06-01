"""Rule-based open-path scenario labels (post-close) for GAME_5M open-gap behavior."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

RULE_VERSION = "open_path_v0"
LABEL_SOURCE = "rule_open_path_v0"

OPEN_PATH_SCENARIOS = (
    "open_follow_through_up",
    "open_gap_up_fade",
    "open_gap_down_bounce",
    "open_gap_down_continuation",
    "open_flat_chop",
    "open_strong_gap_chase",
)


def _cfg_float(key: str, default: float) -> float:
    try:
        from config_loader import get_config_value

        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def close_open_log_return(*, rth_open: float, rth_close: float) -> Optional[float]:
    if rth_open <= 0 or rth_close <= 0:
        return None
    return math.log(rth_close / rth_open)


def close_open_pct(*, rth_open: float, rth_close: float) -> Optional[float]:
    if rth_open <= 0 or rth_close <= 0:
        return None
    return (rth_close / rth_open - 1.0) * 100.0


def fade_from_gap_pct(*, gap_pct: float, close_open_pct_val: float) -> float:
    """How much of the overnight gap was given back by the close (percentage points)."""
    return float(gap_pct) - float(close_open_pct_val)


def classify_open_path_scenario(
    *,
    open_gap_pct: float,
    rth_open: float,
    rth_close: float,
    gap_min_pct: Optional[float] = None,
    strong_gap_pct: Optional[float] = None,
    fade_min_pct: Optional[float] = None,
    follow_through_log: Optional[float] = None,
    bounce_log: Optional[float] = None,
    continuation_log: Optional[float] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Assign one of OPEN_PATH_SCENARIOS from realized open gap and close/open path.

    Priority: flat → strong gap chase → gap-up branch → gap-down branch → flat fallback.
    """
    gap_min = gap_min_pct if gap_min_pct is not None else _cfg_float("OPEN_PATH_RULE_GAP_MIN_PCT", 0.8)
    strong_gap = strong_gap_pct if strong_gap_pct is not None else _cfg_float("OPEN_PATH_RULE_STRONG_GAP_PCT", 4.0)
    fade_min = fade_min_pct if fade_min_pct is not None else _cfg_float("OPEN_PATH_RULE_FADE_MIN_PCT", 1.5)
    ft_log = follow_through_log if follow_through_log is not None else _cfg_float(
        "OPEN_PATH_RULE_FOLLOW_THROUGH_LOG", 0.003
    )
    bounce = bounce_log if bounce_log is not None else _cfg_float("OPEN_PATH_RULE_BOUNCE_LOG", 0.005)
    cont = continuation_log if continuation_log is not None else _cfg_float(
        "OPEN_PATH_RULE_CONTINUATION_LOG", -0.003
    )

    gap = float(open_gap_pct)
    log_ret = close_open_log_return(rth_open=rth_open, rth_close=rth_close)
    co_pct = close_open_pct(rth_open=rth_open, rth_close=rth_close)
    if log_ret is None or co_pct is None:
        raise ValueError("invalid rth_open/rth_close for open-path label")

    fade = fade_from_gap_pct(gap_pct=gap, close_open_pct_val=co_pct)
    meta = {
        "open_gap_pct": round(gap, 4),
        "close_open_log_ret": round(log_ret, 6),
        "close_open_pct": round(co_pct, 4),
        "fade_from_gap_pct": round(fade, 4),
        "rule_version": RULE_VERSION,
    }

    if abs(gap) < gap_min:
        return "open_flat_chop", meta

    if gap >= strong_gap and (fade >= fade_min or rth_close <= rth_open or log_ret <= 0.0):
        return "open_strong_gap_chase", meta

    if gap >= gap_min:
        if log_ret >= ft_log:
            return "open_follow_through_up", meta
        if rth_close <= rth_open or fade >= fade_min:
            return "open_gap_up_fade", meta
        return "open_follow_through_up", meta

    if gap <= -gap_min:
        if log_ret >= bounce:
            return "open_gap_down_bounce", meta
        if log_ret <= cont:
            return "open_gap_down_continuation", meta
        return "open_flat_chop", meta

    return "open_flat_chop", meta

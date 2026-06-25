"""Intraday regime classifier for GAME_5M: chop vs impulse vs fade — entry/exit overlays."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

REGIMES = ("impulse_up", "chop", "fade_extended", "neutral")


def _cfg_bool(key: str, default: bool) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    return raw in ("1", "true", "yes")


def _cfg_float(key: str, default: float) -> float:
    from config_loader import get_config_value

    raw = (get_config_value(key, str(default)) or str(default)).strip().replace(",", ".")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def intraday_regime_enabled() -> bool:
    return _cfg_bool("GAME_5M_INTRADAY_REGIME_ENABLED", True)


def intraday_regime_gate_mode() -> str:
    from config_loader import get_config_value

    mode = (get_config_value("GAME_5M_INTRADAY_REGIME_GATE_MODE", "apply") or "apply").strip().lower()
    return mode if mode in ("apply", "log_only") else "apply"


def regime_label_from_context(ctx: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(ctx, dict):
        return None
    raw = ctx.get("intraday_regime")
    if isinstance(raw, dict):
        lab = raw.get("regime")
        return str(lab).strip() if lab else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def classify_intraday_regime(features: Dict[str, Any]) -> Dict[str, Any]:
    """
  Classify current intraday tape from observable 5m features (no ML).

  Priority: fade_extended → impulse_up → chop → neutral.
  """
    enabled = intraday_regime_enabled()
    mom_rth = features.get("momentum_rth_today_pct")
    mom_2h = features.get("momentum_2h_pct")
    session_move = features.get("session_move_from_open_pct")
    pullback = features.get("pullback_from_high_pct")
    bars_since = features.get("bars_since_session_high")

    impulse_rth_min = _cfg_float("GAME_5M_INTRADAY_REGIME_IMPULSE_RTH_MOMENTUM_MIN_PCT", 2.5)
    impulse_sess_min = _cfg_float("GAME_5M_INTRADAY_REGIME_IMPULSE_SESSION_MOVE_MIN_PCT", 3.0)
    impulse_fresh_high_bars = int(_cfg_float("GAME_5M_INTRADAY_REGIME_IMPULSE_FRESH_HIGH_MAX_BARS", 3))

    chop_rth_max = _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_RTH_MOMENTUM_MAX_PCT", 1.5)
    chop_2h_max = _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_MOMENTUM_2H_MAX_PCT", 0.5)

    fade_sess_min = _cfg_float("GAME_5M_INTRADAY_REGIME_FADE_SESSION_MOVE_MIN_PCT", 2.0)
    fade_pullback_max = _cfg_float("GAME_5M_INTRADAY_REGIME_FADE_MAX_PULLBACK_FROM_HIGH_PCT", 1.5)
    fade_2h_max = _cfg_float("GAME_5M_INTRADAY_REGIME_FADE_MOMENTUM_2H_MAX_PCT", 0.5)

    metrics: Dict[str, Any] = {
        "momentum_rth_today_pct": mom_rth,
        "momentum_2h_pct": mom_2h,
        "session_move_from_open_pct": session_move,
        "pullback_from_high_pct": pullback,
        "bars_since_session_high": bars_since,
    }

    if not enabled:
        return {
            "enabled": False,
            "regime": "neutral",
            "reason": "disabled",
            "metrics": metrics,
            "gate_mode": intraday_regime_gate_mode(),
        }

    regime = "neutral"
    reason = "default"

    # Fade: extended session move but momentum fading at the high.
    if (
        session_move is not None
        and float(session_move) >= fade_sess_min
        and pullback is not None
        and float(pullback) <= fade_pullback_max
        and mom_2h is not None
        and float(mom_2h) <= fade_2h_max
    ):
        regime = "fade_extended"
        reason = (
            f"session +{float(session_move):.2f}% у хая (откат {float(pullback):.2f}%), "
            f"импульс 2ч {float(mom_2h):+.2f}% — затухание"
        )
    elif mom_rth is not None and float(mom_rth) >= impulse_rth_min:
        regime = "impulse_up"
        reason = f"RTH-импульс +{float(mom_rth):.2f}% ≥ {impulse_rth_min}%"
    elif (
        session_move is not None
        and float(session_move) >= impulse_sess_min
        and bars_since is not None
        and int(bars_since) <= impulse_fresh_high_bars
    ):
        regime = "impulse_up"
        reason = (
            f"сессия +{float(session_move):.2f}% ≥ {impulse_sess_min}%, "
            f"свежий high ({int(bars_since)} бар)"
        )
    elif (
        mom_rth is not None
        and float(mom_rth) < chop_rth_max
        and (mom_2h is None or float(mom_2h) < chop_2h_max)
    ):
        regime = "chop"
        reason = (
            f"слабый RTH {float(mom_rth):+.2f}% < {chop_rth_max}% "
            f"и 2ч {float(mom_2h) if mom_2h is not None else 0:+.2f}% < {chop_2h_max}%"
        )

    return {
        "enabled": True,
        "regime": regime,
        "reason": reason,
        "metrics": metrics,
        "gate_mode": intraday_regime_gate_mode(),
    }


def apply_intraday_regime_entry_guard(
    decision: str,
    reasons: list,
    features: Dict[str, Any],
    *,
    technical_entry_branch: Optional[str],
    regime_info: Optional[Dict[str, Any]] = None,
) -> Tuple[str, list, bool, Optional[str], Optional[str]]:
    """
    Entry overlay: chop blocks weak momentum-BUY; fade blocks all new longs.
    Returns (decision, reasons, triggered, prev_decision, guard_reason).
    """
    info = regime_info if isinstance(regime_info, dict) else classify_intraday_regime(features)
    if not info.get("enabled"):
        return decision, reasons, False, None, None

    regime = str(info.get("regime") or "neutral")
    branch = (technical_entry_branch or "").strip()
    mom_rth = features.get("momentum_rth_today_pct")
    chop_entry_min = _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_ENTRY_MOMENTUM_BUY_MIN", 1.5)

    if decision not in ("BUY", "STRONG_BUY"):
        return decision, reasons, False, None, None

    block = False
    guard_reason = ""

    if regime == "fade_extended":
        block = True
        guard_reason = f"intraday regime fade_extended: {info.get('reason')}"
    elif regime == "chop" and branch == "buy_rth_momentum":
        if mom_rth is None or float(mom_rth) < chop_entry_min:
            block = True
            mom_s = f"{float(mom_rth):+.2f}" if mom_rth is not None else "n/a"
            guard_reason = (
                f"intraday regime chop: buy_rth_momentum при RTH {mom_s}% < {chop_entry_min}%"
            )

    if not block:
        return decision, reasons, False, None, None

    apply = intraday_regime_gate_mode() == "apply"
    prev = decision
    if apply:
        decision = "HOLD"
        reasons.append(guard_reason + (" [apply]" if apply else " [log_only]"))
    else:
        reasons.append(guard_reason + " [log_only]")

    return decision, reasons, True, prev, guard_reason


def exit_multipliers_for_regime(regime: Optional[str]) -> Dict[str, float]:
    """Multipliers applied to take cap / momentum factor / soft-take min."""
    lab = (regime or "neutral").strip().lower()
    if lab == "chop":
        return {
            "take_cap_mult": _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_TAKE_CAP_MULT", 0.85),
            "momentum_factor_mult": _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_MOMENTUM_FACTOR_MULT", 0.9),
            "soft_take_min_pct": _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_SOFT_TAKE_MIN_PCT", 2.0),
        }
    if lab == "impulse_up":
        return {
            "take_cap_mult": _cfg_float("GAME_5M_INTRADAY_REGIME_IMPULSE_TAKE_CAP_MULT", 1.0),
            "momentum_factor_mult": _cfg_float("GAME_5M_INTRADAY_REGIME_IMPULSE_MOMENTUM_FACTOR_MULT", 1.15),
            "soft_take_min_pct": _cfg_float("GAME_5M_INTRADAY_REGIME_IMPULSE_SOFT_TAKE_MIN_PCT", 2.5),
        }
    return {
        "take_cap_mult": 1.0,
        "momentum_factor_mult": 1.0,
        "soft_take_min_pct": _cfg_float("GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT", 2.5),
    }


def chop_eod_max_loss_pct() -> float:
    return _cfg_float("GAME_5M_INTRADAY_REGIME_CHOP_EOD_MAX_LOSS_TO_FORCE_PCT", -0.35)


def chop_soft_take_regular_enabled() -> bool:
    return _cfg_bool("GAME_5M_INTRADAY_REGIME_CHOP_SOFT_TAKE_REGULAR_ENABLED", True)

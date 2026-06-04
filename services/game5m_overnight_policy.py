# -*- coding: utf-8 -*-
"""
Политика overnight-риска для GAME_5M: не уносить лонг через закрытие NYSE при неблагоприятном
multiday (1–3d ridge) и при плохом премаркет-гэпе.

Используется в should_close_position (EOD) и send_sndk_signal_cron (блок входа, PRE_MARKET flat).
См. config.env.example (GAME_5M_EOD_*, GAME_5M_PREMARKET_*, GAME_5M_MULTIDAY_OVERNIGHT_*).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _cfg_bool(key: str, default: bool = False) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    return raw in ("1", "true", "yes")


def _cfg_int(key: str, default: int) -> int:
    from config_loader import get_config_value

    try:
        return int((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _cfg_float(key: str, default: float) -> float:
    from config_loader import get_config_value

    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def evaluate_multiday_overnight_risk(d5: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Медвежий multiday для переноса через ночь / принудительного flat.
    Режим GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE; при none — fallback на entry gate (would_hold).
    """
    from services.multiday_lr_gate import evaluate_multiday_entry_gate, evaluate_multiday_overnight_gate

    base: Dict[str, Any] = {
        "source": "none",
        "would_avoid_overnight": False,
        "gate": {},
        "note": None,
    }
    if not isinstance(d5, dict):
        base["note"] = "no_d5_context"
        return base

    og = evaluate_multiday_overnight_gate(d5)
    base["gate"] = og
    mode = (og.get("mode") or "none").strip().lower()
    if mode != "none":
        base["source"] = "overnight_gate"
        base["would_avoid_overnight"] = bool(og.get("would_avoid_overnight"))
        base["note"] = og.get("note")
        return base

    eg = evaluate_multiday_entry_gate(d5)
    base["gate"] = eg
    base["source"] = "entry_gate_fallback"
    base["would_avoid_overnight"] = bool(eg.get("would_hold"))
    base["note"] = eg.get("note") or eg.get("skip_reason")
    return base


def _minutes_until_close(market_session_ctx: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(market_session_ctx, dict):
        return None
    v = market_session_ctx.get("minutes_until_close")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _multiday_bullish_hold_exception(d5: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Разрешить оставить лонг на ночь только при явно бычьем multiday (2+ горизонта)."""
    if not _cfg_bool("GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY", False):
        return False, "hold_exception_disabled"
    if not isinstance(d5, dict):
        return False, "no_d5"
    risk = evaluate_multiday_overnight_risk(d5)
    if risk.get("would_avoid_overnight"):
        return False, "multiday_bearish"
    try:
        from services.multiday_lr_gate import _horizon_pcts

        pcts = _horizon_pcts(d5)
    except Exception:
        return False, "horizons_unavailable"
    tau = _cfg_float("GAME_5M_MULTIDAY_HOLD_TAU_PCT", 0.20)
    pos_min = max(1, min(3, _cfg_int("GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN", 2)))
    positives = sum(1 for k in ("1d", "2d", "3d") if pcts.get(k) is not None and float(pcts[k]) > tau)
    if positives >= pos_min:
        return True, f"bullish_horizons_{positives}>={pos_min}"
    return False, f"not_bullish_enough_{positives}<{pos_min}"


def should_eod_flatten_position(
    *,
    d5: Optional[Dict[str, Any]],
    market_session_ctx: Optional[Dict[str, Any]],
    current_decision: str,
    pnl_current_pct: Optional[float],
    simulation_time: Any = None,
) -> Tuple[bool, str]:
    """
    Принудительный flat перед закрытием RTH.
    Возвращает (should_flat, exit_detail).
    """
    if not _cfg_bool("GAME_5M_EOD_FLATTEN_ENABLED", False):
        return False, ""
    if simulation_time is not None:
        return False, ""

    phase = ""
    if isinstance(market_session_ctx, dict):
        phase = (market_session_ctx.get("session_phase") or "").strip()
    if phase not in ("REGULAR", "NEAR_CLOSE"):
        return False, ""

    mins_left = _minutes_until_close(market_session_ctx)
    window = max(1, min(120, _cfg_int("GAME_5M_EOD_FLATTEN_MINUTES_BEFORE_CLOSE", 20)))
    if mins_left is None or mins_left > float(window):
        return False, ""

    always = _cfg_bool("GAME_5M_EOD_FLATTEN_ALWAYS", True)
    if always:
        return True, "overnight_eod_flat"

    dec = (current_decision or "").strip().upper()
    allow_strong = _cfg_bool("GAME_5M_EOD_FLATTEN_ALLOW_STRONG_BUY_HOLD", False)
    if allow_strong and dec == "STRONG_BUY":
        ok_exc, _ = _multiday_bullish_hold_exception(d5)
        if ok_exc:
            return False, ""

    risk = evaluate_multiday_overnight_risk(d5)
    if risk.get("would_avoid_overnight"):
        return True, "overnight_eod_flat_multiday"

    max_loss_hold = _cfg_float("GAME_5M_EOD_FLATTEN_MAX_LOSS_TO_FORCE_PCT", -0.5)
    if pnl_current_pct is not None and float(pnl_current_pct) <= max_loss_hold:
        return True, "overnight_eod_flat_loss"

    if dec not in ("STRONG_BUY",):
        return True, "overnight_eod_flat_weak_signal"

    return False, ""


def should_block_new_buy_for_overnight(
    d5: Optional[Dict[str, Any]],
    market_session_ctx: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Не открывать новый long, если до закрытия мало времени или multiday медвежий."""
    if not _cfg_bool("GAME_5M_BLOCK_NEW_BUY_NEAR_CLOSE_ENABLED", True):
        pass
    else:
        mins_left = _minutes_until_close(market_session_ctx)
        block_min = max(0, min(240, _cfg_int("GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE", 60)))
        if block_min > 0 and mins_left is not None and mins_left <= float(block_min):
            return True, f"near_close_{mins_left:.0f}min<={block_min}"

    if _cfg_bool("GAME_5M_BLOCK_NEW_BUY_ON_BEARISH_MULTIDAY", True):
        risk = evaluate_multiday_overnight_risk(d5)
        if risk.get("would_avoid_overnight"):
            return True, "bearish_multiday_" + (risk.get("note") or "forecast")

    return False, ""


def should_premarket_auto_flat(
    d5: Optional[Dict[str, Any]],
    *,
    premarket_gap_pct: Optional[float],
) -> Tuple[bool, str]:
    """Закрыть открытый GAME_5M в PRE_MARKET при гэпе вниз и/или медвежьем multiday."""
    if not _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_ENABLED", False):
        return False, ""

    reasons: list[str] = []
    gap_thr = _cfg_float("GAME_5M_PREMARKET_GAP_FLAT_PCT", -2.0)
    use_gap = _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_USE_GAP", True)
    if use_gap and premarket_gap_pct is not None:
        try:
            g = float(premarket_gap_pct)
            if g <= gap_thr:
                reasons.append(f"gap={g:+.2f}%<={gap_thr}%")
        except (TypeError, ValueError):
            pass

    use_md = _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_USE_MULTIDAY", True)
    if use_md:
        risk = evaluate_multiday_overnight_risk(d5)
        if risk.get("would_avoid_overnight"):
            reasons.append(risk.get("note") or "bearish_multiday")

    if not reasons:
        return False, ""
    return True, "; ".join(reasons)


def try_overnight_policy_exit(
    open_position: dict,
    *,
    d5: Optional[Dict[str, Any]],
    current_decision: str,
    current_price: Optional[float],
    session_phase: Optional[str],
    simulation_time: Any = None,
) -> Tuple[bool, str, str]:
    """
    Единая точка для should_close_position: (should_close, signal_type, exit_detail).
    """
    if current_price is None or current_price <= 0:
        return False, "", ""

    entry_price = open_position.get("entry_price")
    pnl_pct: Optional[float] = None
    if isinstance(entry_price, (int, float)) and entry_price > 0:
        pnl_pct = (float(current_price) - float(entry_price)) / float(entry_price) * 100.0

    market_ctx = None
    if simulation_time is None:
        try:
            from services.market_session import get_market_session_context

            market_ctx = get_market_session_context()
        except Exception:
            market_ctx = {"session_phase": session_phase}

    flat, detail = should_eod_flatten_position(
        d5=d5,
        market_session_ctx=market_ctx,
        current_decision=current_decision,
        pnl_current_pct=pnl_pct,
        simulation_time=simulation_time,
    )
    if flat:
        ticker = open_position.get("ticker", "?")
        logger.info(
            "GAME_5M %s: overnight EOD flat — %s, PnL=%.2f%%, decision=%s",
            ticker,
            detail,
            pnl_pct if pnl_pct is not None else 0.0,
            current_decision,
        )
        return True, "TIME_EXIT", detail or "overnight_eod_flat"

    phase = (session_phase or "").strip()
    if phase == "PRE_MARKET" and _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_ENABLED", False):
        gap_pct = None
        if isinstance(d5, dict):
            gap_pct = d5.get("premarket_gap_pct")
        if gap_pct is None:
            try:
                from services.premarket import get_premarket_context

                pm = get_premarket_context(open_position.get("ticker") or "")
                if isinstance(pm, dict):
                    gap_pct = pm.get("premarket_gap_pct")
            except Exception:
                pass
        pm_flat, pm_detail = should_premarket_auto_flat(d5, premarket_gap_pct=gap_pct)
        if pm_flat:
            ticker = open_position.get("ticker", "?")
            logger.info(
                "GAME_5M %s: premarket auto-flat — %s, gap=%s, PnL=%.2f%%",
                ticker,
                pm_detail,
                gap_pct,
                pnl_pct if pnl_pct is not None else 0.0,
            )
            return True, "TIME_EXIT", "overnight_premarket_flat"

    return False, "", ""

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


def _multiday_bullish_hold_exception(
    d5: Optional[Dict[str, Any]],
    *,
    enabled: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Разрешить оставить лонг на ночь только при явно бычьем multiday (2+ горизонта)."""
    if enabled is None:
        enabled = _cfg_bool("GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY", False)
    if not enabled:
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

    ok_exc, exc_reason = _multiday_bullish_hold_exception(d5)
    if ok_exc:
        # Бычий multiday: шанс на overnight / d+1 open; глубокий стоп как у hold_gate.
        deep_loss = _cfg_float("GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT", -4.0)
        if pnl_current_pct is not None and float(pnl_current_pct) <= deep_loss:
            return True, "overnight_eod_flat_loss_deep"
        return False, ""

    max_loss_hold = _cfg_float("GAME_5M_EOD_FLATTEN_MAX_LOSS_TO_FORCE_PCT", -0.5)
    regime_lab = None
    if isinstance(d5, dict):
        try:
            from services.game5m_intraday_regime import chop_eod_max_loss_pct, regime_label_from_context

            regime_lab = regime_label_from_context(d5)
            if regime_lab == "chop":
                max_loss_hold = chop_eod_max_loss_pct()
        except Exception:
            pass

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


def _forecast_regime(d5: Optional[Dict[str, Any]]) -> str:
    if not isinstance(d5, dict):
        return ""
    fl = d5.get("forecast_layer")
    if isinstance(fl, dict):
        reg = fl.get("regime")
        if reg:
            return str(reg).strip()
    reg = d5.get("forecast_regime")
    return str(reg).strip() if reg else ""


def _premarket_flat_triggers(
    d5: Optional[Dict[str, Any]],
    *,
    premarket_gap_pct: Optional[float],
) -> Tuple[list[str], bool, bool]:
    """Сырые триггеры flat: (reasons, gap_trigger, multiday_trigger)."""
    reasons: list[str] = []
    gap_trigger = False
    md_trigger = False

    gap_thr = _cfg_float("GAME_5M_PREMARKET_GAP_FLAT_PCT", -2.0)
    use_gap = _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_USE_GAP", True)
    if use_gap and premarket_gap_pct is not None:
        try:
            g = float(premarket_gap_pct)
            if g <= gap_thr:
                gap_trigger = True
                reasons.append(f"gap={g:+.2f}%<={gap_thr}%")
        except (TypeError, ValueError):
            pass

    use_md = _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_USE_MULTIDAY", True)
    if use_md:
        risk = evaluate_multiday_overnight_risk(d5)
        if risk.get("would_avoid_overnight"):
            md_trigger = True
            reasons.append(risk.get("note") or "bearish_multiday")

    return reasons, gap_trigger, md_trigger


def _premarket_bullish_hold_enabled() -> bool:
    """Premarket hold по бычьему multiday: отдельный флаг или fallback на EOD."""
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_PREMARKET_FLAT_ALLOW_HOLD_ON_BULLISH_MULTIDAY", "") or "").strip().lower()
    if raw in ("1", "true", "yes", "false", "0", "no"):
        return raw in ("1", "true", "yes")
    return _cfg_bool("GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY", False)


def _evaluate_premarket_recovery_gate(
    *,
    open_position: Optional[Dict[str, Any]],
    entry_ctx: Optional[Dict[str, Any]],
    current_price: Optional[float],
    pnl_current_pct: Optional[float],
) -> Dict[str, Any]:
    """Recovery ML для premarket flat: P(отскок) + гварды (shadow/apply)."""
    out: Dict[str, Any] = {
        "enabled": False,
        "status": "disabled",
        "recovery_proba": None,
        "would_defer_by_model": False,
        "would_defer_exit": False,
        "deny_reasons": [],
        "log_only": True,
        "apply_allowed": False,
    }
    if not _cfg_bool("GAME_5M_PREMARKET_RECOVERY_ML_ENABLED", True):
        return out
    out["enabled"] = True

    log_only = _cfg_bool("GAME_5M_RECOVERY_ML_LOG_ONLY", True)
    apply_premarket = _cfg_bool("GAME_5M_PREMARKET_RECOVERY_ML_APPLY", False)
    hold_on_shadow = _cfg_bool("GAME_5M_PREMARKET_RECOVERY_ML_HOLD_ON_WOULD_DEFER", True)
    out["log_only"] = log_only and not apply_premarket
    out["hold_on_shadow"] = hold_on_shadow

    try:
        from services.decision_stack._types import READINESS_PRODUCTION, stack_readiness

        readiness = stack_readiness("recovery_ml")
    except Exception:
        readiness = "telemetry"
    out["readiness"] = readiness
    out["apply_allowed"] = apply_premarket and readiness == READINESS_PRODUCTION

    tau_hold = max(0.0, min(1.0, _cfg_float("GAME_5M_RECOVERY_LIVE_TAU_HOLD", 0.65)))
    hard_stop_loss = _cfg_float("GAME_5M_RECOVERY_HARD_STOP_LOSS_PCT", -3.0)
    deep_loss = _cfg_float("GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT", -4.0)
    out["tau_hold"] = tau_hold

    if not isinstance(open_position, dict) or not isinstance(entry_ctx, dict):
        out["status"] = "skipped"
        out["skip_reason"] = "missing_position_or_entry_ctx"
        return out
    entry_price = open_position.get("entry_price")
    if not isinstance(entry_price, (int, float)) or float(entry_price) <= 0:
        out["status"] = "skipped"
        out["skip_reason"] = "missing_entry_price"
        return out
    ref_close = current_price
    if ref_close is None or float(ref_close) <= 0:
        out["status"] = "skipped"
        out["skip_reason"] = "missing_current_price"
        return out

    deny: list[str] = []
    pnl = pnl_current_pct
    if pnl is None:
        pnl = (float(ref_close) - float(entry_price)) / float(entry_price) * 100.0
    if pnl <= hard_stop_loss:
        deny.append("hard_stop_loss")
    if pnl <= deep_loss:
        deny.append("deep_loss")
    out["deny_reasons"] = deny

    try:
        import pandas as pd

        from config_loader import CHART_DISPLAY_TZ, TRADE_HISTORY_TZ
        from services.game5m_recovery_catboost import (
            default_recovery_catboost_model_path,
            load_recovery_model_meta,
            predict_recovery_hold_proba,
            row_vector_from_hold_bar,
        )

        entry_ts = open_position.get("entry_ts")
        et = pd.Timestamp(entry_ts)
        if et.tzinfo is None:
            et = et.tz_localize(TRADE_HISTORY_TZ, ambiguous=True).tz_convert(CHART_DISPLAY_TZ)
        else:
            et = et.tz_convert(CHART_DISPLAY_TZ)
        bt = pd.Timestamp.now(tz=CHART_DISPLAY_TZ)

        row = row_vector_from_hold_bar(
            ticker=str(open_position.get("ticker") or ""),
            entry_price=float(entry_price),
            entry_ts_et=et,
            bar_time_et=bt,
            ref_close=float(ref_close),
            entry_rsi_5m=entry_ctx.get("rsi_5m"),
            entry_vol_5m_pct=entry_ctx.get("volatility_5m_pct") or entry_ctx.get("entry_volatility_5m_pct"),
            entry_momentum_2h_pct=entry_ctx.get("momentum_2h_pct"),
            entry_decision=entry_ctx.get("decision") or entry_ctx.get("technical_decision_effective"),
        )
        if row is None:
            out["status"] = "skipped"
            out["skip_reason"] = "row_build_failed"
            return out

        from config_loader import get_config_value

        model_path = (get_config_value("GAME_5M_RECOVERY_CATBOOST_MODEL_PATH", "") or "").strip()
        if not model_path:
            model_path = str(default_recovery_catboost_model_path())
        meta = load_recovery_model_meta(model_path) or {}
        pr = predict_recovery_hold_proba(model_path, row, meta=meta if meta else None)
        proba = pr.get("recovery_proba") if isinstance(pr, dict) else None
        out["status"] = pr.get("status") if isinstance(pr, dict) else "error"
        if proba is not None:
            out["recovery_proba"] = round(float(proba), 4)
        would_defer_by_model = proba is not None and float(proba) >= tau_hold
        out["would_defer_by_model"] = bool(would_defer_by_model)
        out["would_defer_exit"] = bool(would_defer_by_model) and not deny
        out["would_hold_premarket"] = bool(
            would_defer_by_model
            and not deny
            and (out["apply_allowed"] or hold_on_shadow)
        )
    except Exception as exc:
        out["status"] = "error"
        out["error"] = str(exc)[:200]

    return out


def evaluate_premarket_flat_decision(
    d5: Optional[Dict[str, Any]],
    *,
    premarket_gap_pct: Optional[float],
    pnl_current_pct: Optional[float] = None,
    entry_ctx: Optional[Dict[str, Any]] = None,
    open_position: Optional[Dict[str, Any]] = None,
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Полная политика premarket flat с учётом forecast/regime, bullish multiday и recovery ML.
    Возвращает should_flat, flat_reasons, hold_reasons, snapshot для context_json.
    """
    snap: Dict[str, Any] = {
        "should_flat": False,
        "flat_reasons": [],
        "hold_reasons": [],
        "forecast_regime": _forecast_regime(d5),
        "premarket_gap_pct": premarket_gap_pct,
        "pnl_current_pct": pnl_current_pct,
    }
    if not _cfg_bool("GAME_5M_PREMARKET_AUTO_FLAT_ENABLED", False):
        snap["note"] = "premarket_auto_flat_disabled"
        return snap

    flat_reasons, gap_trigger, md_trigger = _premarket_flat_triggers(d5, premarket_gap_pct=premarket_gap_pct)
    snap["flat_reasons"] = flat_reasons
    snap["gap_trigger"] = gap_trigger
    snap["multiday_trigger"] = md_trigger
    if not flat_reasons:
        return snap

    # Чистый медвежий multiday без gap — flat без defer.
    if md_trigger and not gap_trigger:
        snap["should_flat"] = True
        snap["flat_reason"] = "; ".join(flat_reasons)
        return snap

    deep_loss = _cfg_float("GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT", -4.0)
    if pnl_current_pct is not None and float(pnl_current_pct) <= deep_loss:
        snap["should_flat"] = True
        snap["flat_reason"] = "; ".join(flat_reasons)
        snap["skipped_hold"] = "deep_loss"
        return snap

    hold_reasons: list[str] = []

    if _premarket_bullish_hold_enabled():
        ok_exc, exc_reason = _multiday_bullish_hold_exception(d5, enabled=True)
        if ok_exc:
            hold_reasons.append(f"bullish_multiday:{exc_reason}")

    if _cfg_bool("GAME_5M_PREMARKET_FLAT_HOLD_ON_GAP_REVERSAL_REGIME", True):
        regime = snap["forecast_regime"]
        if regime == "gap_reversal_opportunity":
            hold_reasons.append("forecast_regime:gap_reversal_opportunity")

    # Премаркет-импульс восстанавливается — не резать на дне гэпа.
    if _cfg_bool("GAME_5M_PREMARKET_FLAT_HOLD_ON_PM_MOMENTUM", True) and isinstance(d5, dict):
        pm_mom = d5.get("premarket_intraday_momentum_pct")
        if pm_mom is None and open_position:
            try:
                from services.premarket import get_premarket_intraday_momentum_pct

                pm_mom = get_premarket_intraday_momentum_pct(str(open_position.get("ticker") or ""))
            except Exception:
                pm_mom = None
        if pm_mom is not None:
            mom_min = _cfg_float("GAME_5M_PREMARKET_MOMENTUM_BUY_MIN", 0.5)
            try:
                if float(pm_mom) >= mom_min:
                    hold_reasons.append(f"premarket_momentum:{float(pm_mom):+.2f}%>={mom_min}%")
                    snap["premarket_intraday_momentum_pct"] = round(float(pm_mom), 4)
            except (TypeError, ValueError):
                pass

    rec = _evaluate_premarket_recovery_gate(
        open_position=open_position,
        entry_ctx=entry_ctx,
        current_price=current_price,
        pnl_current_pct=pnl_current_pct,
    )
    snap["recovery_ml_premarket_flat"] = rec
    if rec.get("would_hold_premarket"):
        proba = rec.get("recovery_proba")
        hold_reasons.append(f"recovery_ml:p={proba}")

    snap["hold_reasons"] = hold_reasons
    if hold_reasons:
        snap["should_flat"] = False
        snap["defer_reason"] = "; ".join(hold_reasons)
        return snap

    snap["should_flat"] = True
    snap["flat_reason"] = "; ".join(flat_reasons)
    return snap


def should_premarket_auto_flat(
    d5: Optional[Dict[str, Any]],
    *,
    premarket_gap_pct: Optional[float],
    pnl_current_pct: Optional[float] = None,
    entry_ctx: Optional[Dict[str, Any]] = None,
    open_position: Optional[Dict[str, Any]] = None,
    current_price: Optional[float] = None,
) -> Tuple[bool, str]:
    """Закрыть открытый GAME_5M в PRE_MARKET при гэпе вниз и/или медвежьем multiday."""
    ev = evaluate_premarket_flat_decision(
        d5,
        premarket_gap_pct=premarket_gap_pct,
        pnl_current_pct=pnl_current_pct,
        entry_ctx=entry_ctx,
        open_position=open_position,
        current_price=current_price,
    )
    if ev.get("should_flat"):
        return True, str(ev.get("flat_reason") or "; ".join(ev.get("flat_reasons") or []))
    return False, ""


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
        pm_flat, pm_detail = should_premarket_auto_flat(
            d5,
            premarket_gap_pct=gap_pct,
            pnl_current_pct=pnl_pct,
            open_position=open_position,
            current_price=current_price,
        )
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


def build_eod_gate_snapshot(
    *,
    d5: Optional[Dict[str, Any]],
    market_session_ctx: Optional[Dict[str, Any]],
    current_decision: str,
    pnl_current_pct: Optional[float],
) -> Dict[str, Any]:
    """Снимок EOD-политики для SELL context_json (бэктест / аудит)."""
    from config_loader import get_config_value
    from services.multiday_lr_gate import _horizon_pcts, build_multiday_trade_context_snapshot

    flat, detail = should_eod_flatten_position(
        d5=d5,
        market_session_ctx=market_session_ctx,
        current_decision=current_decision,
        pnl_current_pct=pnl_current_pct,
    )
    ok_exc, exc_reason = _multiday_bullish_hold_exception(d5)
    risk = evaluate_multiday_overnight_risk(d5)
    md = build_multiday_trade_context_snapshot(d5) if isinstance(d5, dict) else {}
    return {
        "would_flatten": bool(flat),
        "exit_detail": detail or None,
        "bullish_hold_exception": bool(ok_exc),
        "bullish_hold_reason": exc_reason,
        "overnight_risk": risk,
        "horizons_pct": md.get("horizons_pct") or (_horizon_pcts(d5) if isinstance(d5, dict) else {}),
        "config": {
            "eod_flatten_enabled": _cfg_bool("GAME_5M_EOD_FLATTEN_ENABLED", False),
            "eod_flatten_always": _cfg_bool("GAME_5M_EOD_FLATTEN_ALWAYS", True),
            "allow_hold_on_bullish_multiday": _cfg_bool(
                "GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY", False
            ),
            "allow_strong_buy_hold": _cfg_bool("GAME_5M_EOD_FLATTEN_ALLOW_STRONG_BUY_HOLD", False),
            "multiday_overnight_gate_mode": (
                get_config_value("GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE", "none") or "none"
            ),
        },
    }

# -*- coding: utf-8 -*-
"""
Гейты multiday ridge для входа и раннего выхода (фаза D, поэтапно).

Режимы GAME_5M_MULTIDAY_*_GATE_MODE:
  none     — только расчёт прогноза (GAME_5M_MULTIDAY_LR_REG_ENABLED).
  log_only — телеметрия would_hold / would_defer, сделки не меняются.
  apply    — вход: BUY/STRONG_BUY→HOLD; удержание: отложить TIME_EXIT_EARLY (только в кроне).

См. docs/GAME_5M_MULTIDAY_LR_RIDGE.md §7.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_mode(key: str, default: str = "none") -> str:
    from config_loader import get_config_value

    raw = (get_config_value(key, default) or default).strip().lower()
    if raw in ("none", "log_only", "apply"):
        return raw
    if raw in ("log-only", "logonly"):
        return "log_only"
    logger.warning("Неизвестный %s=%r — трактуем как none", key, raw)
    return "none"


def _env_float(key: str, default: float) -> float:
    from config_loader import get_config_value

    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    from config_loader import get_config_value

    try:
        return int((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _horizon_pcts(d5: Dict[str, Any]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for suffix in ("1d", "2d", "3d"):
        k = f"multiday_lr_horizon_{suffix}_pct_vs_spot"
        v = d5.get(k)
        if v is None:
            out[suffix] = None
            continue
        try:
            out[suffix] = float(v)
        except (TypeError, ValueError):
            out[suffix] = None
    return out


def _forecast_ok(d5: Dict[str, Any]) -> bool:
    if d5.get("multiday_lr_forecast_unavailable"):
        return False
    if d5.get("multiday_lr_forecast_error"):
        return False
    pcts = _horizon_pcts(d5)
    return any(v is not None for v in pcts.values())


def evaluate_multiday_entry_gate(d5: Dict[str, Any]) -> Dict[str, Any]:
    """
    Оценка «понизить ли вход» по дневному ridge.
    would_hold=True — при apply режиме effective→HOLD при BUY/STRONG_BUY.
    """
    mode = _env_mode("GAME_5M_MULTIDAY_ENTRY_GATE_MODE", "none")
    tau_1d = _env_float("GAME_5M_MULTIDAY_ENTRY_TAU_1D_PCT", 0.25)
    tau_other = _env_float("GAME_5M_MULTIDAY_ENTRY_TAU_PCT", 0.15)
    neg_min = max(1, min(3, _env_int("GAME_5M_MULTIDAY_ENTRY_NEGATIVE_HORIZONS_MIN", 2)))

    base: Dict[str, Any] = {
        "gate": "entry",
        "mode": mode,
        "tau_1d_pct": tau_1d,
        "tau_pct": tau_other,
        "negative_horizons_min": neg_min,
        "status": "skipped",
        "would_hold": False,
        "applied": False,
        "note": None,
        "horizons_pct": {},
    }

    if mode == "none":
        base["skip_reason"] = "gate_mode_none"
        return base

    if not _forecast_ok(d5):
        base["status"] = "unavailable"
        base["skip_reason"] = "multiday_forecast_unavailable"
        return base

    pcts = _horizon_pcts(d5)
    base["horizons_pct"] = {k: v for k, v in pcts.items() if v is not None}

    h1 = pcts.get("1d")
    negatives = sum(1 for k in ("1d", "2d", "3d") if pcts.get(k) is not None and float(pcts[k]) < -tau_other)
    strong_1d_bear = h1 is not None and float(h1) < -tau_1d
    quorum_bear = negatives >= neg_min

    would_hold = bool(strong_1d_bear or quorum_bear)
    reasons: List[str] = []
    if strong_1d_bear:
        reasons.append(f"1d={h1:+.3f}% < -{tau_1d}%")
    if quorum_bear:
        reasons.append(f"негативных горизонтов {negatives}>={neg_min} (τ={tau_other}%)")

    base["status"] = "ok"
    base["would_hold"] = would_hold
    if would_hold:
        base["note"] = "; ".join(reasons) or "bearish_multiday"
    else:
        base["note"] = "multiday не блокирует вход"

    return base


def evaluate_multiday_overnight_gate(d5: Dict[str, Any]) -> Dict[str, Any]:
    """
    Медвежий multiday → не держать лонг overnight / принудительный EOD flat (mode=apply в кроне).
    Логика та же, что у entry gate; отдельный env для поэтапного rollout.
    """
    mode = _env_mode("GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE", "none")
    tau_1d = _env_float("GAME_5M_MULTIDAY_OVERNIGHT_TAU_1D_PCT", _env_float("GAME_5M_MULTIDAY_ENTRY_TAU_1D_PCT", 0.25))
    tau_other = _env_float("GAME_5M_MULTIDAY_OVERNIGHT_TAU_PCT", _env_float("GAME_5M_MULTIDAY_ENTRY_TAU_PCT", 0.15))
    neg_min = max(
        1,
        min(
            3,
            _env_int(
                "GAME_5M_MULTIDAY_OVERNIGHT_NEGATIVE_HORIZONS_MIN",
                _env_int("GAME_5M_MULTIDAY_ENTRY_NEGATIVE_HORIZONS_MIN", 2),
            ),
        ),
    )

    base: Dict[str, Any] = {
        "gate": "overnight",
        "mode": mode,
        "tau_1d_pct": tau_1d,
        "tau_pct": tau_other,
        "negative_horizons_min": neg_min,
        "status": "skipped",
        "would_avoid_overnight": False,
        "note": None,
        "horizons_pct": {},
    }

    if mode == "none":
        base["skip_reason"] = "gate_mode_none"
        return base

    if not _forecast_ok(d5):
        base["status"] = "unavailable"
        base["skip_reason"] = "multiday_forecast_unavailable"
        return base

    pcts = _horizon_pcts(d5)
    base["horizons_pct"] = {k: v for k, v in pcts.items() if v is not None}

    h1 = pcts.get("1d")
    negatives = sum(1 for k in ("1d", "2d", "3d") if pcts.get(k) is not None and float(pcts[k]) < -tau_other)
    strong_1d_bear = h1 is not None and float(h1) < -tau_1d
    quorum_bear = negatives >= neg_min
    would_avoid = bool(strong_1d_bear or quorum_bear)
    reasons: List[str] = []
    if strong_1d_bear:
        reasons.append(f"1d={h1:+.3f}% < -{tau_1d}%")
    if quorum_bear:
        reasons.append(f"негативных горизонтов {negatives}>={neg_min} (τ={tau_other}%)")

    base["status"] = "ok"
    base["would_avoid_overnight"] = would_avoid
    base["note"] = "; ".join(reasons) if would_avoid else "multiday не блокирует overnight"
    return base


def evaluate_multiday_hold_gate(
    d5: Dict[str, Any],
    *,
    exit_detail: str = "",
    pnl_current_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Оценка «отложить ли TIME_EXIT_EARLY» при бычьем multiday на 2d/3d.
    apply — только в send_sndk_signal_cron (не здесь).
    """
    mode = _env_mode("GAME_5M_MULTIDAY_HOLD_GATE_MODE", "none")
    tau_pos = _env_float("GAME_5M_MULTIDAY_HOLD_TAU_PCT", 0.20)
    pos_min = max(1, min(3, _env_int("GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN", 2)))
    max_loss = _env_float("GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT", -4.0)

    from config_loader import get_config_value

    only_raw = (get_config_value("GAME_5M_MULTIDAY_HOLD_EXIT_DETAILS", "early_derisk,stale_reversal") or "").strip()
    allowed = {x.strip() for x in only_raw.split(",") if x.strip()} if only_raw else set()

    base: Dict[str, Any] = {
        "gate": "hold",
        "mode": mode,
        "tau_pos_pct": tau_pos,
        "positive_horizons_min": pos_min,
        "max_loss_pct": max_loss,
        "exit_detail": exit_detail or "",
        "status": "skipped",
        "would_defer_exit": False,
        "applied": False,
        "note": None,
        "horizons_pct": {},
    }

    if mode == "none":
        base["skip_reason"] = "gate_mode_none"
        return base

    if allowed and (exit_detail or "") not in allowed:
        base["skip_reason"] = f"exit_detail_not_allowed:{exit_detail or ''}"
        return base

    if pnl_current_pct is not None and float(pnl_current_pct) <= max_loss:
        base["skip_reason"] = f"pnl_below_max_loss:{pnl_current_pct:.2f}<={max_loss}"
        return base

    if not _forecast_ok(d5):
        base["status"] = "unavailable"
        base["skip_reason"] = "multiday_forecast_unavailable"
        return base

    pcts = _horizon_pcts(d5)
    base["horizons_pct"] = {k: v for k, v in pcts.items() if v is not None}

    positives = sum(
        1 for k in ("1d", "2d", "3d") if pcts.get(k) is not None and float(pcts[k]) > tau_pos
    )
    would_defer = positives >= pos_min
    base["status"] = "ok"
    base["would_defer_exit"] = would_defer
    if would_defer:
        base["note"] = f"бычьих горизонтов {positives}>={pos_min} (τ=+{tau_pos}%)"
    else:
        base["note"] = "multiday не откладывает ранний выход"

    return base


def hold_gate_should_defer_exit(gate: dict[str, Any]) -> bool:
    """True when hold gate is in apply mode and would defer TIME_EXIT_EARLY."""
    return (
        str(gate.get("mode") or "").strip().lower() == "apply"
        and gate.get("status") == "ok"
        and gate.get("would_defer_exit") is True
    )


def bullish_multiday_horizons_met(d5: Optional[Dict[str, Any]]) -> Tuple[bool, Dict[str, Any]]:
    """2+ горизонта multiday выше τ — бычий тренд для hold/skip early exit."""
    tau = _env_float("GAME_5M_MULTIDAY_HOLD_TAU_PCT", 0.20)
    pos_min = max(1, min(3, _env_int("GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN", 2)))
    meta: Dict[str, Any] = {"tau_pos_pct": tau, "positive_horizons_min": pos_min, "horizons_pct": {}}
    if not isinstance(d5, dict) or not _forecast_ok(d5):
        meta["reason"] = "forecast_unavailable"
        return False, meta
    pcts = _horizon_pcts(d5)
    meta["horizons_pct"] = {k: v for k, v in pcts.items() if v is not None}
    positives = sum(
        1 for k in ("1d", "2d", "3d") if pcts.get(k) is not None and float(pcts[k]) > tau
    )
    meta["positive_horizons"] = positives
    ok = positives >= pos_min
    meta["reason"] = f"bullish_horizons_{positives}>={pos_min}" if ok else f"not_bullish_{positives}<{pos_min}"
    return ok, meta


def should_skip_early_exit_for_bullish_multiday(
    d5: Optional[Dict[str, Any]],
    *,
    exit_detail: str,
    pnl_current_pct: Optional[float],
) -> Tuple[bool, Dict[str, Any]]:
    """
    Не включать TIME_EXIT_EARLY (early_derisk / stale_reversal), если hold gate apply
    отложил бы выход при бычьем multiday.
    """
    gate = evaluate_multiday_hold_gate(
        d5 if isinstance(d5, dict) else {},
        exit_detail=exit_detail,
        pnl_current_pct=pnl_current_pct,
    )
    skip = hold_gate_should_defer_exit(gate)
    return skip, gate


def finalize_technical_decision_with_multiday(out: Dict[str, Any]) -> None:
    """
    После CatBoost fusion: запись телеметрии и опционально HOLD по multiday (mode=apply).
    """
    gate = evaluate_multiday_entry_gate(out)
    out["multiday_lr_entry_gate"] = gate
    out["multiday_lr_entry_gate_mode"] = gate.get("mode")
    out["multiday_lr_entry_gate_status"] = gate.get("status")
    out["multiday_lr_entry_gate_would_hold"] = bool(gate.get("would_hold"))
    out["multiday_lr_entry_gate_applied"] = False
    out["multiday_lr_entry_gate_note"] = gate.get("note")

    mode = gate.get("mode")
    if mode == "none":
        return

    core = out.get("technical_decision_core") or out.get("decision")
    effective = out.get("technical_decision_effective") or core

    if mode == "log_only" and gate.get("would_hold") and effective in ("BUY", "STRONG_BUY"):
        logger.info(
            "MULTIDAY_ENTRY_GATE %s: log_only would_hold=True (%s); effective остаётся %s",
            out.get("ticker") or "?",
            gate.get("note"),
            effective,
        )
        return

    if mode == "apply" and gate.get("status") == "ok" and gate.get("would_hold") and effective in (
        "BUY",
        "STRONG_BUY",
    ):
        out["technical_decision_effective"] = "HOLD"
        out["multiday_lr_entry_gate_applied"] = True
        gate["applied"] = True
        prev = out.get("multiday_lr_entry_gate_note") or ""
        out["multiday_lr_entry_gate_note"] = (prev + " → HOLD (apply)").strip()
        logger.info(
            "MULTIDAY_ENTRY_GATE %s: apply BUY→HOLD (%s)",
            out.get("ticker") or "?",
            gate.get("note"),
        )


def build_multiday_trade_context_snapshot(d5: Dict[str, Any]) -> Dict[str, Any]:
    """Компактный снимок multiday для context_json (BUY/SELL)."""
    pcts = _horizon_pcts(d5)
    return {
        "horizons_pct": {k: v for k, v in pcts.items() if v is not None},
        "bias": d5.get("multiday_lr_bias"),
        "daily_last_date": d5.get("multiday_lr_daily_last_date"),
        "method": d5.get("multiday_lr_method"),
        "forecast_unavailable": bool(d5.get("multiday_lr_forecast_unavailable")),
        "forecast_error": d5.get("multiday_lr_forecast_error"),
        "entry_gate_mode": d5.get("multiday_lr_entry_gate_mode"),
        "entry_gate_would_hold": d5.get("multiday_lr_entry_gate_would_hold"),
    }

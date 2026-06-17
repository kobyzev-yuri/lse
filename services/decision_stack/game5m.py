# -*- coding: utf-8 -*-
"""
GAME_5M: сбор decision_snapshot после существующей цепочки finalize.

Фаза 1: effective = technical_decision_effective (mirror_legacy).
Фаза 3+: DECISION_STACK_RESOLVE_ENABLED — единый resolve_technical.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.decision_stack._types import (
    GAME5M_VETO_ORDER,
    READINESS_CAUTION,
    READINESS_PRODUCTION,
    READINESS_TELEMETRY,
    SCHEMA_VERSION,
    _cfg_bool,
    _cfg_float,
    _utc_now_iso,
    decision_strength_from_signal,
    gate_mode,
    effective_stack_weight,
    make_contribution,
    stack_readiness,
    trust_score_for_contour,
    weight_for_readiness,
)

logger = logging.getLogger(__name__)


def _collect_session_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ms = d5.get("market_session")
    phase = None
    if isinstance(ms, dict):
        phase = ms.get("session_phase") or ms.get("phase")
    elif isinstance(ms, str):
        phase = ms
    if not phase:
        return None
    ph = str(phase).strip().upper()
    if ph in ("REGULAR", "RTH", "OPEN"):
        return make_contribution(
            contour_id="session",
            role="core",
            readiness=READINESS_PRODUCTION,
            strength=0.0,
            weight=1.0,
            action="telemetry",
            detail=f"session_phase={ph}",
            metrics={"session_phase": ph, "gate_mode": "apply"},
        )
    strength = -0.3 if ph in ("PRE_MARKET", "AFTER_HOURS", "CLOSED") else 0.0
    return make_contribution(
        contour_id="session",
        role="core",
        readiness=READINESS_PRODUCTION,
        strength=strength,
        weight=1.0,
        action="veto" if ph not in ("REGULAR", "RTH", "OPEN") else "telemetry",
        detail=f"вне REGULAR: {ph}",
        metrics={"session_phase": ph, "gate_mode": "apply"},
    )


def _collect_rules_contribution(d5: Dict[str, Any]) -> Dict[str, Any]:
    core = d5.get("technical_decision_core") or d5.get("decision") or "HOLD"
    return make_contribution(
        contour_id="rules_5m",
        role="core",
        readiness=READINESS_PRODUCTION,
        strength=decision_strength_from_signal(str(core)),
        weight=1.0,
        action="signal",
        detail=f"technical_decision_core={core}",
        metrics={
            "technical_entry_branch": d5.get("technical_entry_branch"),
            "entry_strong_buy_downgraded": d5.get("entry_strong_buy_downgraded"),
        },
    )


def _collect_kb_news_contribution(d5: Dict[str, Any]) -> Dict[str, Any]:
    imp = str(d5.get("kb_news_impact") or "нейтрально").lower()
    strength = 0.0
    if "негатив" in imp:
        strength = -0.5
    elif "позитив" in imp:
        strength = 0.35
    return make_contribution(
        contour_id="kb_news",
        role="core",
        readiness=READINESS_PRODUCTION,
        strength=strength,
        weight=1.0,
        action="signal" if strength else "telemetry",
        detail=d5.get("kb_news_impact") or "нейтрально",
        metrics={"kb_news_impact": d5.get("kb_news_impact")},
    )


def _collect_entry_advice_contribution(d5: Dict[str, Any]) -> Dict[str, Any]:
    advice = (d5.get("entry_advice") or "ALLOW").strip().upper()
    gm = gate_mode("DECISION_STACK_ENTRY_ADVICE_GATE_MODE", "log_only")
    strength = {"ALLOW": 0.15, "CAUTION": -0.25, "AVOID": -0.7}.get(advice, 0.0)
    would_veto = advice == "AVOID"
    would_down = advice == "CAUTION"
    action = "telemetry"
    if gm == "apply":
        if would_veto:
            action = "veto"
        elif would_down:
            action = "downgrade"
    elif gm != "none" and (would_veto or would_down):
        action = "telemetry"
    return make_contribution(
        contour_id="entry_advice",
        role="policy_gate",
        readiness=READINESS_PRODUCTION,
        strength=strength,
        weight=1.0,
        action=action,
        detail=d5.get("entry_advice_reason") or advice,
        metrics={"entry_advice": advice, "gate_mode": gm, "would_veto": would_veto},
    )


def _collect_macro_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    level = d5.get("macro_risk_level")
    bias = d5.get("macro_equity_gap_bias")
    if level is None and bias is None:
        return None
    strength = 0.0
    if str(bias or "").upper() == "UP":
        strength = 0.2
    elif str(bias or "").upper() == "DOWN":
        strength = -0.25
    if str(level or "").upper() == "AVOID":
        strength = min(strength, -0.5)
    gm = gate_mode("DECISION_STACK_MACRO_GATE_MODE", "log_only")
    lvl = str(level or "").upper()
    would_veto = lvl == "AVOID"
    would_down = lvl == "CAUTION"
    action = "telemetry"
    if gm == "apply":
        if would_veto:
            action = "veto"
        elif would_down:
            action = "downgrade"
    return make_contribution(
        contour_id="macro_risk",
        role="policy_gate",
        readiness=READINESS_PRODUCTION,
        strength=strength,
        weight=1.0,
        action=action,
        detail="; ".join(filter(None, [str(level), str(bias)])),
        metrics={
            "macro_risk_level": level,
            "macro_equity_gap_bias": bias,
            "macro_predicted_sector_gap_pct": d5.get("macro_predicted_sector_gap_pct"),
            "gate_mode": gm,
            "would_veto": would_veto,
        },
    )


def _collect_gap_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pred = d5.get("ticker_open_gap_predicted_pct")
    if pred is None:
        return None
    try:
        p = float(pred)
    except (TypeError, ValueError):
        return None
    readiness = stack_readiness("gap_forecast")
    cid = "gap_forecast"
    return make_contribution(
        contour_id=cid,
        role="model_eval",
        readiness=readiness,
        strength=max(-1.0, min(1.0, p / 3.0)),
        weight=effective_stack_weight(cid, readiness),
        action="telemetry",
        detail=f"pred_open_gap={p:+.2f}% ({d5.get('ticker_open_gap_predicted_source')})",
        metrics={
            "ticker_open_gap_predicted_pct": p,
            "ticker_open_gap_predicted_source": d5.get("ticker_open_gap_predicted_source"),
            "trust_score": trust_score_for_contour(cid),
        },
    )


def _collect_premarket_gap_baseline_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pm = d5.get("premarket_gap_baseline")
    if not isinstance(pm, dict):
        try:
            from services.premarket_gap_baseline import evaluate_premarket_gap_baseline

            pm = evaluate_premarket_gap_baseline(
                d5.get("premarket_gap_pct"),
                very_negative_news=bool(d5.get("very_negative_news")),
                macro_risk_level=d5.get("macro_risk_level"),
                macro_equity_gap_bias=d5.get("macro_equity_gap_bias"),
                multiday_horizon_1d_pct=d5.get("multiday_lr_horizon_1d_pct_vs_spot"),
            )
        except Exception as e:
            logger.debug("premarket_gap_baseline contribution: %s", e)
            pm = None
    if not isinstance(pm, dict):
        return None
    gap = pm.get("premarket_gap_pct")
    try:
        gap_f = float(gap)
    except (TypeError, ValueError):
        return None
    gm = gate_mode("DECISION_STACK_PREMARKET_GAP_BASELINE_GATE_MODE", "apply")
    baseline_action = str(pm.get("action") or "telemetry")
    action = "telemetry"
    if gm == "apply" and baseline_action in ("downgrade", "boost"):
        action = baseline_action
    return make_contribution(
        contour_id="premarket_gap_baseline",
        role="observable_baseline",
        readiness=READINESS_PRODUCTION,
        strength=max(-1.0, min(1.0, gap_f / 4.0)),
        weight=1.0,
        action=action,
        detail=pm.get("reason") or f"premarket_gap={gap_f:+.2f}%",
        metrics={
            "gate_mode": gm,
            "premarket_gap_pct": gap_f,
            "signal": pm.get("signal"),
            "baseline_action": baseline_action,
            "should_boost_entry": bool(pm.get("should_boost_entry")),
            "should_caution_entry": bool(pm.get("should_caution_entry")),
            "should_take_watch": bool(pm.get("should_take_watch")),
            "blocked_by_context": bool(pm.get("blocked_by_context")),
            "thresholds": pm.get("thresholds"),
        },
    )


def _collect_forecast_layer_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fl = d5.get("forecast_layer")
    if not isinstance(fl, dict) or not fl.get("ready"):
        return None
    readiness = stack_readiness("forecast_layer")
    gm = gate_mode("DECISION_STACK_FORECAST_GATE_MODE", "log_only")
    regime = str(fl.get("regime") or "neutral_or_unavailable")
    og = fl.get("open_gap") if isinstance(fl.get("open_gap"), dict) else {}
    horizons = fl.get("horizons_pct") if isinstance(fl.get("horizons_pct"), dict) else {}
    h_vals: List[float] = []
    for key in ("1d", "2d", "3d"):
        v = horizons.get(key) if isinstance(horizons, dict) else None
        try:
            if v is not None:
                h_vals.append(float(v))
        except (TypeError, ValueError):
            pass
    md_avg = sum(h_vals) / len(h_vals) if h_vals else None
    gap_pred = None
    try:
        if og.get("predicted_pct") is not None:
            gap_pred = float(og.get("predicted_pct"))
    except (TypeError, ValueError):
        gap_pred = None
    chase_gap = _cfg_float("DECISION_STACK_FORECAST_CHASE_GAP_MIN_PCT", 1.0)
    bearish_md = md_avg is not None and md_avg < -_cfg_float("DECISION_STACK_FORECAST_BEARISH_MULTIDAY_PCT", 0.15)
    would_down = regime in ("aligned_bearish", "gap_fade_risk") or (
        gap_pred is not None and gap_pred >= chase_gap and bearish_md
    )
    opp = fl.get("gap_up_opportunity") if isinstance(fl.get("gap_up_opportunity"), dict) else {}
    would_boost = bool(opp.get("should_boost_entry"))
    action = "telemetry"
    if gm == "apply" and would_down:
        action = "downgrade"
    elif would_boost:
        action = "boost" if gm == "apply" else "telemetry"
    strength_raw = md_avg if md_avg is not None else gap_pred
    strength = 0.0
    if strength_raw is not None:
        strength = max(-1.0, min(1.0, float(strength_raw) / 2.0))
    if would_down and strength > -0.1:
        strength = -0.35
    if would_boost and not would_down and strength < 0.25:
        strength = 0.35
    return make_contribution(
        contour_id="forecast_layer",
        role="policy_gate",
        readiness=readiness,
        strength=strength,
        weight=weight_for_readiness(readiness),
        action=action,
        detail=f"regime={regime}, gap={gap_pred}, md_avg={md_avg}",
        metrics={
            "gate_mode": gm,
            "regime": regime,
            "would_downgrade": would_down,
            "would_boost_entry": would_boost,
            "gap_up_opportunity": opp,
            "forecast_open_gap_pct": gap_pred,
            "multiday_avg_pct": round(md_avg, 4) if md_avg is not None else None,
            "confidence": og.get("confidence") if isinstance(og, dict) else None,
        },
    )


def _collect_catboost_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fusion_mode = (d5.get("catboost_fusion_mode") or "none").strip().lower()
    if fusion_mode == "none" and d5.get("catboost_signal_status") not in ("ok",):
        return None
    core = d5.get("technical_decision_core")
    eff = d5.get("technical_decision_effective")
    p = d5.get("catboost_entry_proba_good")
    readiness = stack_readiness("catboost_entry_5m")
    stack_gm = gate_mode("DECISION_STACK_CATBOOST_GATE_MODE", "apply")
    would_down = False
    if fusion_mode == "hold_if_buy_below_p" and core in ("BUY", "STRONG_BUY") and p is not None:
        try:
            p_min = _cfg_float("GAME_5M_CATBOOST_HOLD_BELOW_P", 0.45)
            would_down = float(p) < p_min
        except (TypeError, ValueError):
            would_down = False
    if core in ("BUY", "STRONG_BUY") and eff == "HOLD" and fusion_mode != "none":
        would_down = True
    action = "telemetry"
    if stack_gm == "apply" and would_down:
        action = "downgrade"
    elif stack_gm != "none" and would_down:
        action = "telemetry"
    cid = "catboost_entry_5m"
    return make_contribution(
        contour_id=cid,
        role="policy_gate",
        readiness=readiness,
        strength=(float(p) - 0.5) * 2 if p is not None else 0.0,
        weight=effective_stack_weight(cid, readiness),
        action=action,
        detail=d5.get("catboost_fusion_note") or f"P={p}, fusion={fusion_mode}",
        metrics={
            "catboost_entry_proba_good": p,
            "catboost_fusion_mode": fusion_mode,
            "catboost_signal_status": d5.get("catboost_signal_status"),
            "gate_mode": stack_gm,
            "would_downgrade": would_down,
            "trust_score": trust_score_for_contour(cid),
        },
    )


def _collect_multiday_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gate = d5.get("multiday_lr_entry_gate")
    if not isinstance(gate, dict):
        return None
    readiness = stack_readiness("multiday_lr")
    would = bool(gate.get("would_hold"))
    mode = gate.get("mode") or gate_mode("GAME_5M_MULTIDAY_ENTRY_GATE_MODE", "none")
    stack_gm = gate_mode("DECISION_STACK_MULTIDAY_GATE_MODE", "apply")
    action = "telemetry"
    if would and mode == "apply" and stack_gm == "apply":
        action = "downgrade"
    elif would and mode == "apply" and stack_gm == "log_only":
        action = "telemetry"
    h1 = d5.get("multiday_lr_horizon_1d_pct_vs_spot")
    strength = 0.0
    if h1 is not None:
        try:
            strength = max(-1.0, min(1.0, float(h1) / 2.0))
        except (TypeError, ValueError):
            pass
    cid = "multiday_lr"
    return make_contribution(
        contour_id=cid,
        role="policy_gate",
        readiness=readiness,
        strength=strength,
        weight=effective_stack_weight(cid, readiness),
        action=action,
        detail=gate.get("note") or f"mode={mode}, would_hold={would}",
        metrics={
            "multiday_lr_entry_gate_mode": mode,
            "multiday_lr_entry_gate_would_hold": would,
            "horizons_pct": gate.get("horizons_pct"),
            "stack_gate_mode": stack_gm,
            "applied_legacy": bool(d5.get("multiday_lr_entry_gate_applied")),
            "trust_score": trust_score_for_contour(cid),
        },
    )


def _collect_news_fusion_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    m = d5.get("entry_fusion_metrics")
    if not isinstance(m, dict) or m.get("fused_bias_neg1") is None:
        return None
    readiness = stack_readiness("news_fusion")
    try:
        fused = float(m["fused_bias_neg1"])
    except (TypeError, ValueError):
        return None
    gm = gate_mode("DECISION_STACK_NEWS_FUSION_GATE_MODE", "log_only")
    veto_below = _cfg_float("DECISION_STACK_NEWS_FUSION_VETO_BELOW", -0.35)
    boost_above = _cfg_float("DECISION_STACK_NEWS_FUSION_BOOST_ABOVE", 0.25)
    would_down = fused <= veto_below
    would_veto = fused <= veto_below - 0.15
    action = "telemetry"
    if gm == "apply":
        if would_veto:
            action = "veto"
        elif would_down:
            action = "downgrade"
    elif gm != "none" and would_down:
        action = "telemetry"
    metrics = dict(m)
    metrics.update(
        {
            "gate_mode": gm,
            "veto_below": veto_below,
            "boost_above": boost_above,
            "would_downgrade": would_down,
        }
    )
    return make_contribution(
        contour_id="news_fusion",
        role="policy_gate",
        readiness=readiness,
        strength=max(-1.0, min(1.0, fused)),
        weight=weight_for_readiness(readiness),
        action=action,
        detail=f"fused_bias={fused:+.3f} tech={m.get('tech_bias_neg1')} news={m.get('news_bias_kb')}",
        metrics=metrics,
    )


def _collect_earnings_trust_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ticker = str(d5.get("ticker") or "").strip().upper()
    if not ticker:
        return None
    try:
        from services.earnings_trust_runtime import (
            build_earnings_trust_runtime,
            earnings_trust_gate_mode,
        )

        runtime = build_earnings_trust_runtime(ticker)
    except Exception as e:
        logger.debug("earnings_trust runtime %s: %s", ticker, e)
        return None
    if not runtime.get("active"):
        return None

    gm = earnings_trust_gate_mode()
    readiness = stack_readiness("earnings_trust")
    cid = "earnings_trust"
    strength = float(runtime.get("strength") or 0.0)
    would_down = bool(runtime.get("would_downgrade"))
    action = "telemetry"
    if gm == "apply" and would_down:
        action = "downgrade"
    elif gm == "apply" and strength > 0.2:
        action = "boost"

    role = str(runtime.get("runtime_role") or "source")
    trust_mult = trust_score_for_contour(
        "peer_spillover" if role == "peer" else "earnings_scenario"
    )

    return make_contribution(
        contour_id=cid,
        role="advisory_postmortem",
        readiness=readiness,
        strength=max(-1.0, min(1.0, strength)),
        weight=round(weight_for_readiness(readiness) * trust_mult, 4),
        action=action,
        detail=str(runtime.get("detail_ru") or ""),
        metrics={
            "gate_mode": gm,
            "runtime_role": role,
            "source_symbol": runtime.get("source_symbol"),
            "event_date": runtime.get("event_date"),
            "would_downgrade": would_down,
            "trust_score": trust_mult,
            "trust_labels": runtime.get("trust_labels"),
            "fusion": runtime.get("fusion"),
            "fusion_outcome": runtime.get("fusion_outcome"),
            "rolling_degradation": runtime.get("rolling_degradation"),
            "postmortem_version": runtime.get("postmortem_version"),
        },
    )


def collect_game5m_contributions(d5: Dict[str, Any], *, ticker: str = "") -> List[Dict[str, Any]]:
    """Собирает все известные контуры из полей d5 (после finalize)."""
    out: List[Dict[str, Any]] = []
    collectors = (
        _collect_session_contribution,
        _collect_rules_contribution,
        _collect_kb_news_contribution,
        _collect_entry_advice_contribution,
        _collect_macro_contribution,
        _collect_premarket_gap_baseline_contribution,
        _collect_forecast_layer_contribution,
        _collect_gap_contribution,
        _collect_news_fusion_contribution,
        _collect_catboost_contribution,
        _collect_multiday_contribution,
        _collect_earnings_trust_contribution,
    )
    for fn in collectors:
        try:
            c = fn(d5)
            if c is not None:
                out.append(c)
        except Exception as e:
            logger.debug("decision_stack contribution %s %s: %s", ticker, getattr(fn, "__name__", fn), e)
    return out


def _detect_conflicts(contributions: List[Dict[str, Any]], core: str) -> List[str]:
    conflicts: List[str] = []
    core_bull = core in ("BUY", "STRONG_BUY")
    for c in contributions:
        if c.get("action") in ("veto", "downgrade") and core_bull and float(c.get("strength") or 0) < -0.2:
            conflicts.append(f"{c.get('contour_id')}: {c.get('detail')}")
    return conflicts


def summarize_earnings_trust_impact(
    contributions: List[Dict[str, Any]],
    *,
    core: str,
    legacy_eff: str,
    projected: str,
) -> Dict[str, Any]:
    """Компактный блок для мониторинга влияния earnings_trust на resolve (shadow/live)."""
    et = next((c for c in contributions if c.get("contour_id") == "earnings_trust"), None)
    if not et:
        return {"active": False}

    metrics = et.get("metrics") if isinstance(et.get("metrics"), dict) else {}
    action = str(et.get("action") or "telemetry")
    gm = str(metrics.get("gate_mode") or "log_only")
    resolve_on = _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False)
    core_u = str(core or "HOLD").upper()
    legacy_u = str(legacy_eff or core_u).upper()
    projected_u = str(projected or legacy_u).upper()
    bull_core = core_u in ("BUY", "STRONG_BUY")
    would_apply_down = gm == "apply" and action == "downgrade" and bull_core
    et_caused_projected = (
        action in ("veto", "downgrade")
        and gm == "apply"
        and legacy_u in ("BUY", "STRONG_BUY")
        and projected_u == "HOLD"
    )

    return {
        "active": True,
        "gate_mode": gm,
        "resolve_enabled": resolve_on,
        "action": action,
        "strength": et.get("strength"),
        "weight": et.get("weight"),
        "detail": et.get("detail"),
        "runtime_role": metrics.get("runtime_role"),
        "source_symbol": metrics.get("source_symbol"),
        "event_date": metrics.get("event_date"),
        "would_downgrade": bool(metrics.get("would_downgrade")),
        "trust_labels": metrics.get("trust_labels"),
        "shadow_would_hold_if_core_bull": would_apply_down,
        "live_would_hold_if_core_bull": would_apply_down and resolve_on,
        "changed_projected_resolve": et_caused_projected,
        "core_decision": core_u,
        "legacy_effective": legacy_u,
        "projected_effective": projected_u,
    }


def _apply_contribution_to_effective(effective: str, c: Dict[str, Any]) -> str:
    if effective not in ("BUY", "STRONG_BUY"):
        return effective
    action = c.get("action")
    if action == "veto":
        return "HOLD"
    if action == "downgrade":
        return "HOLD"
    return effective


def resolve_game5m_technical(
    d5: Dict[str, Any],
    contributions: List[Dict[str, Any]],
) -> str:
    """
    Фаза 2–3: пересчёт effective из contributions (только action=veto|downgrade при gate apply).
    """
    core = str(d5.get("technical_decision_core") or d5.get("decision") or "HOLD")
    effective = core
    by_id = {c.get("contour_id"): c for c in contributions if c.get("contour_id")}
    for cid in GAME5M_VETO_ORDER:
        c = by_id.get(cid)
        if not c:
            continue
        if cid in ("news_fusion", "forecast_layer", "gap_forecast", "catboost_entry_5m", "multiday_lr"):
            if c.get("readiness") != READINESS_PRODUCTION:
                continue
        metrics = c.get("metrics") if isinstance(c.get("metrics"), dict) else {}
        gm = metrics.get("gate_mode") or metrics.get("stack_gate_mode")
        if cid == "catboost_entry_5m" and not gm:
            gm = gate_mode("DECISION_STACK_CATBOOST_GATE_MODE", "apply")
        if cid == "multiday_lr" and not gm:
            gm = gate_mode("DECISION_STACK_MULTIDAY_GATE_MODE", "apply")
        if cid == "forecast_layer" and not gm:
            gm = gate_mode("DECISION_STACK_FORECAST_GATE_MODE", "log_only")
        if cid == "earnings_trust" and not gm:
            from services.earnings_trust_runtime import earnings_trust_gate_mode

            gm = earnings_trust_gate_mode()
        if cid == "session":
            gm = "apply"
        if gm in (None, "", "none", "log_only"):
            continue
        effective = _apply_contribution_to_effective(effective, c)
    return effective


def apply_resolve_to_d5(d5: Dict[str, Any], effective: str, contributions: List[Dict[str, Any]]) -> None:
    """Синхронизация полей d5 после resolve (вход крона смотрит effective/decision)."""
    prev = d5.get("technical_decision_effective")
    d5["technical_decision_effective"] = effective
    d5["decision_effective"] = effective
    core = d5.get("technical_decision_core") or d5.get("decision")
    if effective != core and effective == "HOLD" and core in ("BUY", "STRONG_BUY"):
        d5["decision_stack_downgraded"] = True
    by_id = {c.get("contour_id"): c for c in contributions}
    md = by_id.get("multiday_lr")
    if (
        md
        and md.get("action") == "downgrade"
        and isinstance(md.get("metrics"), dict)
        and md["metrics"].get("multiday_lr_entry_gate_mode") == "apply"
    ):
        d5["multiday_lr_entry_gate_applied"] = True
        gate = d5.get("multiday_lr_entry_gate")
        if isinstance(gate, dict):
            gate["applied"] = True
    cb = by_id.get("catboost_entry_5m")
    if cb and cb.get("action") == "downgrade" and prev in ("BUY", "STRONG_BUY") and effective == "HOLD":
        if not d5.get("catboost_fusion_note"):
            d5["catboost_fusion_note"] = "decision_stack resolve → HOLD"


def build_game5m_decision_snapshot(
    d5: Dict[str, Any],
    *,
    ticker: str = "",
) -> Dict[str, Any]:
    contributions = collect_game5m_contributions(d5, ticker=ticker)
    core = str(d5.get("technical_decision_core") or d5.get("decision") or "HOLD")
    legacy_eff = str(d5.get("technical_decision_effective") or core)
    projected = resolve_game5m_technical(d5, contributions)
    resolve_on = _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False)
    if resolve_on:
        effective = projected
        mode = "resolve_technical"
        if effective != legacy_eff:
            logger.info(
                "decision_stack %s: resolve %s → %s (legacy was %s)",
                ticker,
                legacy_eff,
                effective,
                legacy_eff,
            )
    else:
        effective = legacy_eff
        mode = "mirror_legacy"
    conflicts = _detect_conflicts(contributions, core)
    diverged = projected != legacy_eff
    et_impact = summarize_earnings_trust_impact(
        contributions, core=core, legacy_eff=legacy_eff, projected=projected
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "game": "GAME_5M",
        "ticker": (ticker or d5.get("ticker") or "").strip().upper(),
        "ts_utc": _utc_now_iso(),
        "core_decision": core,
        "effective_decision": effective,
        "projected_effective_if_resolve": projected,
        "resolve_mode": mode,
        "resolve_divergence": diverged,
        "contributions": contributions,
        "conflicts": conflicts,
        "earnings_trust_impact": et_impact,
        "gate_modes": {
            "entry_advice": gate_mode("DECISION_STACK_ENTRY_ADVICE_GATE_MODE", "log_only"),
            "macro": gate_mode("DECISION_STACK_MACRO_GATE_MODE", "log_only"),
            "premarket_gap_baseline": gate_mode("DECISION_STACK_PREMARKET_GAP_BASELINE_GATE_MODE", "apply"),
            "news_fusion": gate_mode("DECISION_STACK_NEWS_FUSION_GATE_MODE", "log_only"),
            "forecast": gate_mode("DECISION_STACK_FORECAST_GATE_MODE", "log_only"),
            "catboost": gate_mode("DECISION_STACK_CATBOOST_GATE_MODE", "apply"),
            "multiday": gate_mode("DECISION_STACK_MULTIDAY_GATE_MODE", "apply"),
            "earnings_trust": gate_mode("DECISION_STACK_EARNINGS_TRUST_GATE_MODE", "log_only"),
        },
        "llm_eligible": [
            "news_fusion",
            "cluster_context",
            "boss_brief",
            "kb_news",
            "macro_risk",
        ],
        "legacy": {
            "technical_decision_effective": legacy_eff,
            "decision": d5.get("decision"),
        },
    }


def attach_entry_fusion_metrics(d5: Dict[str, Any], *, ticker: str, kb_news: Optional[List[Dict[str, Any]]] = None) -> None:
    """Опционально: tech+news fusion для контура news_fusion (фаза 2)."""
    try:
        from services.llm_service import build_entry_fusion_metrics

        td = {
            "technical_signal": d5.get("technical_decision_core") or d5.get("decision"),
            "momentum_2h_pct": d5.get("momentum_2h_pct"),
            "kb_news_days": d5.get("kb_news_days"),
        }
        news = kb_news if kb_news is not None else []
        sent = 0.5
        imp = str(d5.get("kb_news_impact") or "")
        if "негатив" in imp:
            sent = 0.35
        elif "позитив" in imp:
            sent = 0.65
        d5["entry_fusion_metrics"] = build_entry_fusion_metrics(ticker, td, news, sent)
    except Exception as e:
        logger.debug("attach_entry_fusion_metrics %s: %s", ticker, e)


def finalize_game5m_decision_stack(
    d5: Dict[str, Any],
    *,
    ticker: str = "",
    kb_news: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    Единая финализация: ML-гейты (фаза 3) → fusion (фаза 2) → snapshot → опционально resolve.
    """
    if not _cfg_bool("DECISION_STACK_ENABLED", True):
        return
    t = (ticker or d5.get("ticker") or "").strip().upper()
    if t:
        d5["ticker"] = t
    from services.decision_stack.game5m_policy import apply_game5m_policy_gates, stack_own_finalize_enabled

    if stack_own_finalize_enabled():
        apply_game5m_policy_gates(d5, t)
    attach_entry_fusion_metrics(d5, ticker=t, kb_news=kb_news)
    snap = build_game5m_decision_snapshot(d5, ticker=t)
    contributions = snap.get("contributions") or []
    d5["decision_snapshot"] = snap
    d5["decision_effective"] = snap.get("effective_decision")
    d5["decision_stack_version"] = SCHEMA_VERSION
    d5["decision_stack_projected_effective"] = snap.get("projected_effective_if_resolve")
    if _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False):
        eff = snap.get("effective_decision") or "HOLD"
        apply_resolve_to_d5(d5, str(eff), contributions)
        d5["decision"] = d5.get("technical_decision_effective")

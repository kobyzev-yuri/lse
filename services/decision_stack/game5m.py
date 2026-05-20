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
    _utc_now_iso,
    decision_strength_from_signal,
    default_readiness,
    make_contribution,
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
            metrics={"session_phase": ph},
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
        metrics={"session_phase": ph},
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
    strength = {"ALLOW": 0.15, "CAUTION": -0.25, "AVOID": -0.7}.get(advice, 0.0)
    action = "veto" if advice == "AVOID" else ("downgrade" if advice == "CAUTION" else "telemetry")
    return make_contribution(
        contour_id="entry_advice",
        role="policy_gate",
        readiness=READINESS_PRODUCTION,
        strength=strength,
        weight=1.0,
        action=action,
        detail=d5.get("entry_advice_reason") or advice,
        metrics={"entry_advice": advice},
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
    return make_contribution(
        contour_id="macro_risk",
        role="policy_gate",
        readiness=READINESS_PRODUCTION,
        strength=strength,
        weight=1.0,
        action="downgrade" if str(level or "").upper() in ("AVOID", "CAUTION") else "telemetry",
        detail="; ".join(filter(None, [str(level), str(bias)])),
        metrics={
            "macro_risk_level": level,
            "macro_equity_gap_bias": bias,
            "macro_predicted_sector_gap_pct": d5.get("macro_predicted_sector_gap_pct"),
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
    readiness = default_readiness("gap_forecast")
    return make_contribution(
        contour_id="gap_forecast",
        role="model_eval",
        readiness=readiness,
        strength=max(-1.0, min(1.0, p / 3.0)),
        weight=weight_for_readiness(readiness),
        action="telemetry",
        detail=f"pred_open_gap={p:+.2f}% ({d5.get('ticker_open_gap_predicted_source')})",
        metrics={
            "ticker_open_gap_predicted_pct": p,
            "ticker_open_gap_predicted_source": d5.get("ticker_open_gap_predicted_source"),
        },
    )


def _collect_catboost_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mode = (d5.get("catboost_fusion_mode") or "none").strip().lower()
    if mode == "none" and d5.get("catboost_signal_status") not in ("ok",):
        return None
    core = d5.get("technical_decision_core")
    eff = d5.get("technical_decision_effective")
    p = d5.get("catboost_entry_proba_good")
    readiness = default_readiness("catboost_entry_5m")
    action = "telemetry"
    if core in ("BUY", "STRONG_BUY") and eff == "HOLD" and mode != "none":
        action = "downgrade"
    return make_contribution(
        contour_id="catboost_entry_5m",
        role="policy_gate",
        readiness=readiness,
        strength=(float(p) - 0.5) * 2 if p is not None else 0.0,
        weight=weight_for_readiness(readiness),
        action=action,
        detail=d5.get("catboost_fusion_note") or f"P={p}, mode={mode}",
        metrics={
            "catboost_entry_proba_good": p,
            "catboost_fusion_mode": mode,
            "catboost_signal_status": d5.get("catboost_signal_status"),
        },
    )


def _collect_multiday_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    gate = d5.get("multiday_lr_entry_gate")
    if not isinstance(gate, dict):
        return None
    readiness = default_readiness("multiday_lr")
    would = bool(gate.get("would_hold"))
    mode = gate.get("mode") or "none"
    action = "telemetry"
    if would and mode == "apply":
        action = "downgrade"
    elif would and mode == "log_only":
        action = "telemetry"
    h1 = d5.get("multiday_lr_horizon_1d_pct_vs_spot")
    strength = 0.0
    if h1 is not None:
        try:
            strength = max(-1.0, min(1.0, float(h1) / 2.0))
        except (TypeError, ValueError):
            pass
    return make_contribution(
        contour_id="multiday_lr",
        role="policy_gate",
        readiness=readiness,
        strength=strength,
        weight=weight_for_readiness(readiness),
        action=action,
        detail=gate.get("note") or f"mode={mode}, would_hold={would}",
        metrics={
            "multiday_lr_entry_gate_mode": mode,
            "multiday_lr_entry_gate_would_hold": would,
            "horizons_pct": gate.get("horizons_pct"),
        },
    )


def _collect_news_fusion_contribution(d5: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    m = d5.get("entry_fusion_metrics")
    if not isinstance(m, dict) or m.get("fused_bias_neg1") is None:
        return None
    readiness = default_readiness("news_fusion")
    try:
        fused = float(m["fused_bias_neg1"])
    except (TypeError, ValueError):
        return None
    return make_contribution(
        contour_id="news_fusion",
        role="policy_gate",
        readiness=readiness,
        strength=max(-1.0, min(1.0, fused)),
        weight=weight_for_readiness(readiness),
        action="telemetry",
        detail=f"fused_bias={fused:+.3f} tech={m.get('tech_bias_neg1')} news={m.get('news_bias_kb')}",
        metrics=m,
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
        _collect_gap_contribution,
        _collect_news_fusion_contribution,
        _collect_catboost_contribution,
        _collect_multiday_contribution,
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


def resolve_game5m_technical(
    d5: Dict[str, Any],
    contributions: List[Dict[str, Any]],
) -> str:
    """
    Фаза 3+: пересчёт effective из contributions.
    Пока дублирует mirror — при resolve_enabled проверяем согласованность.
    """
    core = str(d5.get("technical_decision_core") or d5.get("decision") or "HOLD")
    effective = core
    for cid in GAME5M_VETO_ORDER:
        for c in contributions:
            if c.get("contour_id") != cid:
                continue
            if c.get("readiness") == READINESS_TELEMETRY:
                continue
            if c.get("action") == "veto" and effective in ("BUY", "STRONG_BUY"):
                effective = "HOLD"
            elif c.get("action") == "downgrade" and effective in ("BUY", "STRONG_BUY"):
                if cid in ("catboost_entry_5m", "multiday_lr") or c.get("contour_id") == "entry_advice":
                    effective = "HOLD"
    return effective


def build_game5m_decision_snapshot(
    d5: Dict[str, Any],
    *,
    ticker: str = "",
) -> Dict[str, Any]:
    contributions = collect_game5m_contributions(d5, ticker=ticker)
    core = str(d5.get("technical_decision_core") or d5.get("decision") or "HOLD")
    legacy_eff = str(d5.get("technical_decision_effective") or core)
    resolve_on = _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False)
    if resolve_on:
        effective = resolve_game5m_technical(d5, contributions)
        mode = "resolve_technical"
        if effective != legacy_eff:
            logger.warning(
                "decision_stack %s: resolve=%s legacy_effective=%s (проверьте порядок veto)",
                ticker,
                effective,
                legacy_eff,
            )
    else:
        effective = legacy_eff
        mode = "mirror_legacy"
    conflicts = _detect_conflicts(contributions, core)
    return {
        "schema_version": SCHEMA_VERSION,
        "game": "GAME_5M",
        "ticker": (ticker or d5.get("ticker") or "").strip().upper(),
        "ts_utc": _utc_now_iso(),
        "core_decision": core,
        "effective_decision": effective,
        "resolve_mode": mode,
        "contributions": contributions,
        "conflicts": conflicts,
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
    Вызывать в конце get_decision_5m после CatBoost/multiday finalize.
    Пишет decision_snapshot; при mirror не меняет decision/effective.
    """
    if not _cfg_bool("DECISION_STACK_ENABLED", True):
        return
    t = (ticker or d5.get("ticker") or "").strip().upper()
    attach_entry_fusion_metrics(d5, ticker=t, kb_news=kb_news)
    snap = build_game5m_decision_snapshot(d5, ticker=t)
    d5["decision_snapshot"] = snap
    d5["decision_effective"] = snap.get("effective_decision")
    d5["decision_stack_version"] = SCHEMA_VERSION
    if _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False):
        d5["technical_decision_effective"] = snap.get("effective_decision")
        if snap.get("effective_decision") != d5.get("decision"):
            d5["decision"] = snap.get("effective_decision")

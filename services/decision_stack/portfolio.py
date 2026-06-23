# -*- coding: utf-8 -*-
"""PORTFOLIO: unified decision snapshot and shadow resolve."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.decision_stack._types import (
    PORTFOLIO_VETO_ORDER,
    READINESS_PRODUCTION,
    SCHEMA_VERSION,
    _cfg_bool,
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


def collect_portfolio_contributions(
    decision: Dict[str, Any],
    *,
    portfolio_ml: Optional[Dict[str, Any]] = None,
    event_reaction: Optional[Dict[str, Any]] = None,
    cluster_context: Optional[Dict[str, Any]] = None,
    multiday: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    sig = decision.get("decision") or decision.get("strategy_signal") or "HOLD"
    strat = decision.get("selected_strategy") or decision.get("strategy_name")
    out.append(
        make_contribution(
            contour_id="strategy_rules",
            role="core",
            readiness=stack_readiness("strategy_rules"),
            strength=decision_strength_from_signal(str(sig)),
            weight=1.0,
            action="signal",
            detail=f"strategy={strat}, signal={sig}",
            metrics={"selected_strategy": strat},
        )
    )
    llm_dec = decision.get("decision_fused") or decision.get("llm_decision")
    if llm_dec and str(llm_dec).strip().upper() != str(sig).strip().upper():
        out.append(
            make_contribution(
                contour_id="llm_fusion",
                role="model_eval",
                readiness=stack_readiness("news_fusion"),
                strength=decision_strength_from_signal(str(llm_dec)),
                weight=0.2,
                action="telemetry",
                detail=f"llm={llm_dec}, strategy={sig}",
                metrics={"decision_fused": decision.get("decision_fused"), "llm_decision": decision.get("llm_decision")},
            )
        )

    pm = portfolio_ml or {}
    score = pm.get("portfolio_ml_entry_score")
    status = pm.get("portfolio_ml_status")
    if score is not None or status:
        readiness = stack_readiness("portfolio_catboost")
        try:
            sc = float(score)
            strength = (sc - 50.0) / 50.0
        except (TypeError, ValueError):
            sc = None
            strength = 0.0
        gm = gate_mode("DECISION_STACK_PORTFOLIO_CATBOOST_GATE_MODE", "log_only")
        min_score = 48.0
        try:
            from config_loader import get_config_value

            min_score = float((get_config_value("PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE", "48") or "48").strip())
        except Exception:
            pass
        would_veto = status == "ok" and sc is not None and sc < min_score
        action = "telemetry"
        if would_veto and gm == "apply" and readiness == READINESS_PRODUCTION:
            action = "veto"
        cid = "portfolio_catboost"
        out.append(
            make_contribution(
                contour_id=cid,
                role="policy_gate",
                readiness=readiness,
                strength=max(-1.0, min(1.0, strength)),
                weight=effective_stack_weight(cid, readiness),
                action=action,
                detail=pm.get("portfolio_ml_note") or f"score={score}, status={status}",
                metrics={
                    "portfolio_ml_entry_score": score,
                    "portfolio_ml_expected_return_pct": pm.get("portfolio_ml_expected_return_pct"),
                    "portfolio_ml_status": status,
                    "gate_mode": gm,
                    "min_score": min_score,
                    "would_veto": would_veto,
                    "trust_score": trust_score_for_contour(cid),
                },
            )
        )
    er = event_reaction or {}
    er_status = er.get("event_reaction_ml_status")
    er_score = er.get("event_reaction_ml_entry_score")
    if er_status or er_score is not None:
        readiness = stack_readiness("event_reaction")
        try:
            sc = float(er_score)
            strength = (sc - 50.0) / 50.0
        except (TypeError, ValueError):
            sc = None
            strength = 0.0
        gm = gate_mode("DECISION_STACK_EVENT_REACTION_GATE_MODE", "log_only")
        min_score = 48.0
        try:
            from config_loader import get_config_value

            min_score = float((get_config_value("EVENT_REACTION_HOLD_BELOW_SCORE", "48") or "48").strip())
        except Exception:
            pass
        would_veto = er_status == "ok" and sc is not None and sc < min_score
        action = "telemetry"
        if would_veto and gm == "apply" and readiness == READINESS_PRODUCTION:
            action = "veto"
        cid = "event_reaction"
        out.append(
            make_contribution(
                contour_id=cid,
                role="policy_gate",
                readiness=readiness,
                strength=max(-1.0, min(1.0, strength)),
                weight=effective_stack_weight(cid, readiness),
                action=action,
                detail=er.get("event_reaction_ml_note") or f"event_score={er_score}, status={er_status}",
                metrics={
                    "event_reaction_ml_entry_score": er_score,
                    "event_reaction_ml_direction": er.get("event_reaction_ml_direction"),
                    "event_reaction_ml_forward_log_ret_5d_pred": er.get("event_reaction_ml_forward_log_ret_5d_pred"),
                    "event_reaction_ml_expected_return_5d_pct": er.get("event_reaction_ml_expected_return_5d_pct"),
                    "event_reaction_ml_event_time_et": er.get("event_reaction_ml_event_time_et"),
                    "event_reaction_ml_status": er_status,
                    "gate_mode": gm,
                    "min_score": min_score,
                    "would_veto": would_veto,
                    "trust_score": trust_score_for_contour(cid),
                },
            )
        )
    md = multiday or {}
    if md.get("portfolio_multiday_status") == "ok" or md.get("multiday_lr_horizon_1d_pct_vs_spot") is not None:
        from services.multiday_lr_gate import evaluate_multiday_entry_gate

        gate = evaluate_multiday_entry_gate(
            md,
            mode_env_key="PORTFOLIO_MULTIDAY_ENTRY_GATE_MODE",
            tau_1d_env_key="PORTFOLIO_MULTIDAY_ENTRY_TAU_1D_PCT",
            tau_other_env_key="PORTFOLIO_MULTIDAY_ENTRY_TAU_PCT",
            neg_min_env_key="PORTFOLIO_MULTIDAY_ENTRY_NEGATIVE_HORIZONS_MIN",
        )
        h1 = md.get("multiday_lr_horizon_1d_pct_vs_spot")
        try:
            strength = max(-1.0, min(1.0, float(h1) / 2.0)) if h1 is not None else 0.0
        except (TypeError, ValueError):
            strength = 0.0
        gm = gate_mode("PORTFOLIO_MULTIDAY_ENTRY_GATE_MODE", "log_only")
        would_veto = bool(gate.get("would_hold"))
        action = "telemetry"
        if would_veto and gm == "apply":
            action = "veto"
        cid = "multiday_lr"
        out.append(
            make_contribution(
                contour_id=cid,
                role="policy_gate",
                readiness=stack_readiness("multiday_lr"),
                strength=strength,
                weight=effective_stack_weight(cid, stack_readiness("multiday_lr")),
                action=action,
                detail=gate.get("note") or md.get("log_return_multiday_forecast_summary"),
                metrics={
                    "horizons_pct": gate.get("horizons_pct"),
                    "would_hold": would_veto,
                    "gate_mode": gm,
                    "trust_score": trust_score_for_contour(cid),
                },
            )
        )
    cc = cluster_context or decision.get("cluster")
    if isinstance(cc, dict):
        corr = cc.get("correlation_this_ticker") or {}
        other = cc.get("other_signals_at_decision") or cc.get("other_signals") or {}
        n_buys = sum(1 for v in (other or {}).values() if str(v).upper() in ("BUY", "STRONG_BUY"))
        detail = f"cluster tickers={len(cc.get('tickers') or [])}, prior_buy_signals={n_buys}"
        out.append(
            make_contribution(
                contour_id="cluster_context",
                role="policy_gate",
                readiness=stack_readiness("cluster_context"),
                strength=-0.15 if n_buys >= 2 else 0.0,
                weight=weight_for_readiness(stack_readiness("cluster_context")),
                action="telemetry",
                detail=detail,
                metrics={
                    "prior_buy_signals": n_buys,
                    "correlation_this_ticker": corr,
                    "other_signals_at_decision": other,
                },
            )
        )
    return out


def _resolve_portfolio_decision(core: str, contributions: List[Dict[str, Any]]) -> str:
    effective = str(core or "HOLD").upper()
    if effective not in ("BUY", "STRONG_BUY"):
        return effective
    by_id = {c.get("contour_id"): c for c in contributions if c.get("contour_id")}
    for cid in PORTFOLIO_VETO_ORDER:
        c = by_id.get(cid)
        if not c:
            continue
        if c.get("action") in ("veto", "downgrade"):
            return "HOLD"
    return effective


def _top_reasons(contributions: List[Dict[str, Any]], *, limit: int = 3) -> List[str]:
    ranked = sorted(
        contributions,
        key=lambda c: (
            0 if c.get("action") in ("veto", "downgrade") else 1,
            -abs(float(c.get("strength") or 0.0)),
        ),
    )
    out: List[str] = []
    for c in ranked:
        detail = str(c.get("detail") or "").strip()
        if detail:
            out.append(f"{c.get('contour_id')}: {detail}")
        if len(out) >= limit:
            break
    return out


def build_portfolio_decision_snapshot(
    decision: Dict[str, Any],
    *,
    ticker: str = "",
    portfolio_ml: Optional[Dict[str, Any]] = None,
    event_reaction: Optional[Dict[str, Any]] = None,
    cluster_context: Optional[Dict[str, Any]] = None,
    multiday: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contributions = collect_portfolio_contributions(
        decision,
        portfolio_ml=portfolio_ml,
        event_reaction=event_reaction,
        cluster_context=cluster_context,
        multiday=multiday,
    )
    core = str(decision.get("decision") or "HOLD").upper()
    legacy_eff = str(decision.get("decision_fused") or decision.get("llm_decision") or core).upper()
    projected = _resolve_portfolio_decision(core, contributions)
    resolve_on = _cfg_bool("DECISION_STACK_PORTFOLIO_RESOLVE_ENABLED", False)
    effective = projected if resolve_on else legacy_eff
    conflicts = _top_reasons(
        [c for c in contributions if c.get("action") in ("veto", "downgrade")],
        limit=5,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "game": "PORTFOLIO",
        "ticker": ticker.strip().upper(),
        "ts_utc": _utc_now_iso(),
        "core_decision": core,
        "effective_decision": effective,
        "projected_effective_if_resolve": projected,
        "resolve_mode": "portfolio_resolve" if resolve_on else "mirror_legacy",
        "resolve_divergence": projected != legacy_eff,
        "contributions": contributions,
        "conflicts": conflicts,
        "gate_modes": {
            "portfolio_catboost": gate_mode("DECISION_STACK_PORTFOLIO_CATBOOST_GATE_MODE", "log_only"),
            "event_reaction": gate_mode("DECISION_STACK_EVENT_REACTION_GATE_MODE", "log_only"),
        },
        "llm_eligible": ["news_fusion", "cluster_context", "boss_brief", "portfolio_catboost"],
        "legacy": {
            "decision": decision.get("decision"),
            "decision_fused": decision.get("decision_fused"),
            "llm_decision": decision.get("llm_decision"),
        },
        "verdict": build_unified_verdict_from_snapshot(
            {
                "core_decision": core,
                "effective_decision": effective,
                "projected_effective_if_resolve": projected,
                "resolve_divergence": projected != legacy_eff,
                "contributions": contributions,
            }
        ),
    }


def finalize_portfolio_decision_stack(
    decision: Dict[str, Any],
    *,
    ticker: str = "",
    portfolio_ml: Optional[Dict[str, Any]] = None,
    event_reaction: Optional[Dict[str, Any]] = None,
    cluster_context: Optional[Dict[str, Any]] = None,
    multiday: Optional[Dict[str, Any]] = None,
) -> None:
    if not _cfg_bool("DECISION_STACK_ENABLED", True):
        return
    snap = build_portfolio_decision_snapshot(
        decision,
        ticker=ticker,
        portfolio_ml=portfolio_ml,
        event_reaction=event_reaction,
        cluster_context=cluster_context,
        multiday=multiday,
    )
    decision["decision_snapshot"] = snap
    decision["decision_effective"] = snap.get("effective_decision")
    decision["decision_stack_projected_effective"] = snap.get("projected_effective_if_resolve")
    decision["decision_stack_version"] = SCHEMA_VERSION
    decision["decision_verdict"] = snap.get("verdict")


def build_unified_verdict_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    effective = str(snapshot.get("effective_decision") or "HOLD").upper()
    projected = str(snapshot.get("projected_effective_if_resolve") or effective).upper()
    contributions = snapshot.get("contributions") if isinstance(snapshot.get("contributions"), list) else []
    vetoes = [c for c in contributions if c.get("action") in ("veto", "downgrade")]
    positives = [c for c in contributions if float(c.get("strength") or 0.0) > 0.2]
    cautions = [c for c in contributions if float(c.get("strength") or 0.0) < -0.2]
    if effective in ("BUY", "STRONG_BUY"):
        label = "Trade"
    elif projected == "HOLD" and vetoes:
        label = "Avoid"
    elif cautions:
        label = "Wait"
    elif effective in ("SELL", "STRONG_SELL"):
        label = "Exit"
    else:
        label = "Hold"
    reasons = _top_reasons(vetoes or cautions or positives or contributions, limit=3)
    return {
        "label": label,
        "effective_decision": effective,
        "projected_effective_if_resolve": projected,
        "resolve_divergence": bool(snapshot.get("resolve_divergence")),
        "primary_reasons": reasons,
    }

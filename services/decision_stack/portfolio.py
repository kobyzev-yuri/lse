# -*- coding: utf-8 -*-
"""PORTFOLIO: заготовка decision_snapshot (фаза 4)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.decision_stack._types import (
    SCHEMA_VERSION,
    _cfg_bool,
    _utc_now_iso,
    decision_strength_from_signal,
    default_readiness,
    make_contribution,
    weight_for_readiness,
)

logger = logging.getLogger(__name__)


def collect_portfolio_contributions(
    decision: Dict[str, Any],
    *,
    portfolio_ml: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    sig = decision.get("decision") or decision.get("strategy_signal") or "HOLD"
    strat = decision.get("selected_strategy") or decision.get("strategy_name")
    out.append(
        make_contribution(
            contour_id="strategy_rules",
            role="core",
            readiness=default_readiness("strategy_rules"),
            strength=decision_strength_from_signal(str(sig)),
            weight=1.0,
            action="signal",
            detail=f"strategy={strat}, signal={sig}",
            metrics={"selected_strategy": strat},
        )
    )
    pm = portfolio_ml or {}
    score = pm.get("portfolio_ml_entry_score")
    status = pm.get("portfolio_ml_status")
    if score is not None or status:
        readiness = default_readiness("portfolio_catboost")
        try:
            sc = float(score)
            strength = (sc - 50.0) / 50.0
        except (TypeError, ValueError):
            strength = 0.0
        out.append(
            make_contribution(
                contour_id="portfolio_catboost",
                role="policy_gate",
                readiness=readiness,
                strength=max(-1.0, min(1.0, strength)),
                weight=weight_for_readiness(readiness),
                action="telemetry",
                detail=pm.get("portfolio_ml_note") or f"score={score}, status={status}",
                metrics={
                    "portfolio_ml_entry_score": score,
                    "portfolio_ml_expected_return_pct": pm.get("portfolio_ml_expected_return_pct"),
                    "portfolio_ml_status": status,
                },
            )
        )
    return out


def build_portfolio_decision_snapshot(
    decision: Dict[str, Any],
    *,
    ticker: str = "",
    portfolio_ml: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contributions = collect_portfolio_contributions(decision, portfolio_ml=portfolio_ml)
    core = str(decision.get("decision") or "HOLD")
    effective = str(decision.get("decision_fused") or decision.get("llm_decision") or core)
    return {
        "schema_version": SCHEMA_VERSION,
        "game": "PORTFOLIO",
        "ticker": ticker.strip().upper(),
        "ts_utc": _utc_now_iso(),
        "core_decision": core,
        "effective_decision": effective,
        "resolve_mode": "mirror_legacy",
        "contributions": contributions,
        "conflicts": [],
        "llm_eligible": ["news_fusion", "cluster_context", "boss_brief", "portfolio_catboost"],
    }


def finalize_portfolio_decision_stack(
    decision: Dict[str, Any],
    *,
    ticker: str = "",
    portfolio_ml: Optional[Dict[str, Any]] = None,
) -> None:
    if not _cfg_bool("DECISION_STACK_ENABLED", True):
        return
    snap = build_portfolio_decision_snapshot(decision, ticker=ticker, portfolio_ml=portfolio_ml)
    decision["decision_snapshot"] = snap
    decision["decision_effective"] = snap.get("effective_decision")
    decision["decision_stack_version"] = SCHEMA_VERSION

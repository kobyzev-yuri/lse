# -*- coding: utf-8 -*-
"""Legacy GAME_5M entry guards — mirror decision_stack gates without RESOLVE."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _gate_mode(key: str, default: str = "log_only") -> str:
    try:
        from config_loader import get_config_value

        raw = (get_config_value(key, default) or default).strip().lower()
    except Exception:
        raw = default
    if raw in ("none", "log_only", "apply"):
        return raw
    return default


def _effective_bullish(out: Dict[str, Any]) -> Optional[str]:
    effective = out.get("technical_decision_effective") or out.get("decision")
    if effective in ("BUY", "STRONG_BUY"):
        return str(effective)
    return None


def evaluate_entry_advice_entry_guard(out: Dict[str, Any]) -> Dict[str, Any]:
    """CAUTION/AVOID → would_hold when GAME_5M_ENTRY_ADVICE_GATE_MODE=apply."""
    mode = _gate_mode("GAME_5M_ENTRY_ADVICE_GATE_MODE", "log_only")
    advice = (out.get("entry_advice") or "ALLOW").strip().upper()
    would_hold = advice in ("CAUTION", "AVOID")
    gate: Dict[str, Any] = {
        "mode": mode,
        "entry_advice": advice,
        "would_hold": would_hold and _effective_bullish(out) is not None,
        "applied": False,
        "note": out.get("entry_advice_reason") or advice,
    }
    if mode == "log_only" and gate["would_hold"]:
        logger.info(
            "ENTRY_ADVICE_GATE %s: log_only would_hold=True (%s)",
            out.get("ticker") or "?",
            gate["note"],
        )
    return gate


def _premarket_gap_baseline_dict(out: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pm = out.get("premarket_gap_baseline")
    if isinstance(pm, dict):
        return pm
    if out.get("premarket_gap_pct") is None:
        return None
    try:
        from services.premarket_gap_baseline import evaluate_premarket_gap_baseline

        pm = evaluate_premarket_gap_baseline(
            out.get("premarket_gap_pct"),
            very_negative_news=bool(out.get("very_negative_news")),
            macro_risk_level=out.get("macro_risk_level"),
            macro_equity_gap_bias=out.get("macro_equity_gap_bias"),
            multiday_horizon_1d_pct=out.get("multiday_lr_horizon_1d_pct_vs_spot"),
        )
    except Exception as e:
        logger.debug("premarket_gap_entry_guard evaluate: %s", e)
        return None
    if isinstance(pm, dict):
        out["premarket_gap_baseline"] = pm
    return pm if isinstance(pm, dict) else None


def evaluate_premarket_gap_entry_guard(out: Dict[str, Any]) -> Dict[str, Any]:
    """premarket_gap_baseline downgrade → would_hold when gate=apply."""
    mode = _gate_mode("GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE", "log_only")
    pm = _premarket_gap_baseline_dict(out)
    baseline_action = str((pm or {}).get("action") or "telemetry")
    would_hold = (
        baseline_action == "downgrade"
        and _effective_bullish(out) is not None
    )
    gate: Dict[str, Any] = {
        "mode": mode,
        "baseline_action": baseline_action,
        "premarket_gap_pct": (pm or {}).get("premarket_gap_pct"),
        "signal": (pm or {}).get("signal"),
        "would_hold": would_hold,
        "applied": False,
        "note": (pm or {}).get("reason") or "premarket_gap_baseline",
    }
    if mode == "log_only" and would_hold:
        logger.info(
            "PREMARKET_GAP_GATE %s: log_only would_hold=True (%s)",
            out.get("ticker") or "?",
            gate["note"],
        )
    return gate


def finalize_technical_decision_with_entry_guards(out: Dict[str, Any]) -> None:
    """
    После CatBoost fusion и multiday: опционально HOLD по entry_advice и premarket gap.
    Порядок как в decision_stack: entry_advice → premarket_gap_baseline.
    """
    advice_gate = evaluate_entry_advice_entry_guard(out)
    gap_gate = evaluate_premarket_gap_entry_guard(out)
    out["game5m_entry_advice_entry_guard"] = advice_gate
    out["game5m_premarket_gap_entry_guard"] = gap_gate
    out["game5m_entry_guard_applied"] = False
    out["game5m_entry_guard_note"] = None

    effective = out.get("technical_decision_effective") or out.get("decision")
    notes: list[str] = []

    if advice_gate.get("mode") == "apply" and advice_gate.get("would_hold") and effective in (
        "BUY",
        "STRONG_BUY",
    ):
        effective = "HOLD"
        advice_gate["applied"] = True
        notes.append(f"entry_advice={advice_gate.get('entry_advice')} → HOLD")

    if gap_gate.get("mode") == "apply" and gap_gate.get("would_hold") and effective in (
        "BUY",
        "STRONG_BUY",
    ):
        effective = "HOLD"
        gap_gate["applied"] = True
        notes.append(str(gap_gate.get("note") or "premarket_gap_baseline downgrade"))

    if notes:
        out["technical_decision_effective"] = effective
        out["game5m_entry_guard_applied"] = True
        out["game5m_entry_guard_note"] = "; ".join(notes)
        logger.info(
            "ENTRY_GUARD %s: apply → HOLD (%s)",
            out.get("ticker") or "?",
            out["game5m_entry_guard_note"],
        )

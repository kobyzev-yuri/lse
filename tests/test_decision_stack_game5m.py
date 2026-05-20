# -*- coding: utf-8 -*-
"""Тесты decision_stack GAME_5M (фаза 1: mirror legacy)."""

from __future__ import annotations

from services.decision_stack.game5m import (
    build_game5m_decision_snapshot,
    collect_game5m_contributions,
    finalize_game5m_decision_stack,
)


def test_mirror_legacy_effective():
    d5 = {
        "decision": "BUY",
        "technical_decision_core": "BUY",
        "technical_decision_effective": "HOLD",
        "catboost_fusion_mode": "hold_if_buy_below_p",
        "catboost_fusion_note": "P=0.3",
        "catboost_entry_proba_good": 0.3,
        "catboost_signal_status": "ok",
        "entry_advice": "ALLOW",
        "kb_news_impact": "нейтрально",
        "multiday_lr_entry_gate": {
            "mode": "log_only",
            "would_hold": True,
            "note": "bearish",
            "horizons_pct": {"1d": -0.5},
        },
    }
    snap = build_game5m_decision_snapshot(d5, ticker="NVDA")
    assert snap["effective_decision"] == "HOLD"
    assert snap["resolve_mode"] == "mirror_legacy"
    assert snap["game"] == "GAME_5M"
    ids = {c["contour_id"] for c in snap["contributions"]}
    assert "rules_5m" in ids
    assert "catboost_entry_5m" in ids
    assert "multiday_lr" in ids


def test_finalize_attaches_snapshot():
    d5 = {
        "decision": "BUY",
        "technical_decision_core": "BUY",
        "technical_decision_effective": "BUY",
        "entry_advice": "CAUTION",
        "entry_advice_reason": "вола",
        "kb_news_impact": "нейтрально",
    }
    finalize_game5m_decision_stack(d5, ticker="AAPL", kb_news=[])
    assert "decision_snapshot" in d5
    assert d5["decision_effective"] == "BUY"
    assert d5["decision_stack_version"] == 1


def test_contributions_entry_advice_avoid():
    d5 = {
        "technical_decision_core": "BUY",
        "entry_advice": "AVOID",
        "entry_advice_reason": "macro",
    }
    contribs = collect_game5m_contributions(d5, ticker="X")
    adv = next(c for c in contribs if c["contour_id"] == "entry_advice")
    assert adv["action"] == "veto"
    assert adv["strength"] < 0

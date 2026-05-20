# -*- coding: utf-8 -*-
"""Тесты decision_stack GAME_5M (фазы 1–3)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.decision_stack.game5m import (
    build_game5m_decision_snapshot,
    collect_game5m_contributions,
    finalize_game5m_decision_stack,
    resolve_game5m_technical,
)
from services.decision_stack.game5m_policy import apply_game5m_policy_gates


class TestDecisionStackGame5m(unittest.TestCase):
    def test_mirror_legacy_effective(self):
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
        self.assertEqual(snap["effective_decision"], "HOLD")
        self.assertEqual(snap["resolve_mode"], "mirror_legacy")
        self.assertEqual(snap["projected_effective_if_resolve"], "HOLD")
        ids = {c["contour_id"] for c in snap["contributions"]}
        self.assertIn("rules_5m", ids)
        self.assertIn("catboost_entry_5m", ids)

    def test_finalize_attaches_snapshot(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "entry_advice": "CAUTION",
            "entry_advice_reason": "вола",
            "kb_news_impact": "нейтрально",
        }
        with patch("services.decision_stack.game5m_policy.stack_own_finalize_enabled", return_value=False):
            finalize_game5m_decision_stack(d5, ticker="AAPL", kb_news=[])
        self.assertIn("decision_snapshot", d5)
        self.assertEqual(d5["decision_effective"], "BUY")
        self.assertEqual(d5["decision_stack_version"], 1)

    def test_resolve_entry_advice_avoid_apply(self):
        env = {
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "apply",
            "DECISION_STACK_MACRO_GATE_MODE": "none",
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
        }
        d5 = {
            "technical_decision_core": "BUY",
            "entry_advice": "AVOID",
            "entry_advice_reason": "macro",
            "kb_news_impact": "нейтрально",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            contribs = collect_game5m_contributions(d5, ticker="X")
            eff = resolve_game5m_technical(d5, contribs)
        self.assertEqual(eff, "HOLD")
        adv = next(c for c in contribs if c["contour_id"] == "entry_advice")
        self.assertEqual(adv["action"], "veto")

    def test_news_fusion_downgrade_when_apply(self):
        env = {
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "apply",
            "DECISION_STACK_NEWS_FUSION_VETO_BELOW": "-0.35",
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
        }
        d5 = {
            "technical_decision_core": "STRONG_BUY",
            "entry_fusion_metrics": {
                "fused_bias_neg1": -0.5,
                "tech_bias_neg1": 0.2,
                "news_bias_kb": -0.8,
            },
            "entry_advice": "ALLOW",
            "kb_news_impact": "негатив",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, str(d) if d is not None else "")):
            contribs = collect_game5m_contributions(d5)
            eff = resolve_game5m_technical(d5, contribs)
        self.assertEqual(eff, "HOLD")

    def test_projected_differs_from_legacy(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "entry_advice": "AVOID",
            "entry_advice_reason": "test",
            "kb_news_impact": "нейтрально",
        }
        env = {"DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "apply"}
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            snap = build_game5m_decision_snapshot(d5, ticker="T")
        self.assertTrue(snap["resolve_divergence"])
        self.assertEqual(snap["projected_effective_if_resolve"], "HOLD")
        self.assertEqual(snap["effective_decision"], "BUY")


if __name__ == "__main__":
    unittest.main()

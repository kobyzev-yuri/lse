# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from services.game5m_entry_guards import finalize_technical_decision_with_entry_guards
from services.premarket_gap_baseline import evaluate_premarket_gap_baseline


class Game5mEntryGuardsTests(unittest.TestCase):
    def test_premarket_gap_negative_apply_hold(self):
        env = {
            "GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE": "apply",
            "GAME_5M_ENTRY_ADVICE_GATE_MODE": "none",
        }
        out = {
            "ticker": "MU",
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "premarket_gap_pct": -2.4,
            "entry_advice": "ALLOW",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            finalize_technical_decision_with_entry_guards(out)
        self.assertEqual(out["technical_decision_effective"], "HOLD")
        self.assertTrue(out["game5m_entry_guard_applied"])
        self.assertTrue(out["game5m_premarket_gap_entry_guard"]["applied"])
        pm = evaluate_premarket_gap_baseline(-2.4)
        self.assertEqual(pm["action"], "downgrade")

    def test_premarket_gap_log_only_keeps_buy(self):
        env = {
            "GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE": "log_only",
            "GAME_5M_ENTRY_ADVICE_GATE_MODE": "none",
        }
        out = {
            "ticker": "AMD",
            "decision": "BUY",
            "technical_decision_effective": "BUY",
            "premarket_gap_pct": -4.0,
            "entry_advice": "ALLOW",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            finalize_technical_decision_with_entry_guards(out)
        self.assertEqual(out["technical_decision_effective"], "BUY")
        self.assertFalse(out["game5m_entry_guard_applied"])
        self.assertTrue(out["game5m_premarket_gap_entry_guard"]["would_hold"])

    def test_entry_advice_caution_apply_hold(self):
        env = {
            "GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE": "none",
            "GAME_5M_ENTRY_ADVICE_GATE_MODE": "apply",
        }
        out = {
            "ticker": "LITE",
            "decision": "BUY",
            "technical_decision_effective": "BUY",
            "entry_advice": "CAUTION",
            "entry_advice_reason": "вола",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            finalize_technical_decision_with_entry_guards(out)
        self.assertEqual(out["technical_decision_effective"], "HOLD")
        self.assertTrue(out["game5m_entry_advice_entry_guard"]["applied"])

    def test_entry_advice_avoid_apply_hold(self):
        env = {
            "GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE": "none",
            "GAME_5M_ENTRY_ADVICE_GATE_MODE": "apply",
        }
        out = {
            "ticker": "CIEN",
            "decision": "STRONG_BUY",
            "technical_decision_effective": "STRONG_BUY",
            "entry_advice": "AVOID",
            "entry_advice_reason": "macro",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            finalize_technical_decision_with_entry_guards(out)
        self.assertEqual(out["technical_decision_effective"], "HOLD")

    def test_strong_gap_up_without_fade_risk_not_blocked(self):
        env = {
            "GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE": "apply",
            "GAME_5M_ENTRY_ADVICE_GATE_MODE": "none",
        }
        out = {
            "ticker": "NVDA",
            "decision": "BUY",
            "technical_decision_effective": "BUY",
            "premarket_gap_pct": 4.6,
            "entry_advice": "ALLOW",
            "kb_news_impact": "нейтрально",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            finalize_technical_decision_with_entry_guards(out)
        self.assertEqual(out["technical_decision_effective"], "BUY")
        self.assertFalse(out["game5m_premarket_gap_entry_guard"]["would_hold"])


if __name__ == "__main__":
    unittest.main()

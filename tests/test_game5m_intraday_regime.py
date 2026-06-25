# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from services.game5m_intraday_regime import (
    apply_intraday_regime_entry_guard,
    classify_intraday_regime,
    exit_multipliers_for_regime,
    regime_label_from_context,
)


class TestClassifyIntradayRegime(unittest.TestCase):
    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=True)
    def test_impulse_up_by_rth(self, _en):
        info = classify_intraday_regime({"momentum_rth_today_pct": 3.0, "momentum_2h_pct": 1.0})
        self.assertEqual(info["regime"], "impulse_up")

    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=True)
    def test_chop_weak_momentum(self, _en):
        info = classify_intraday_regime(
            {"momentum_rth_today_pct": 1.2, "momentum_2h_pct": 0.3, "session_move_from_open_pct": 0.5}
        )
        self.assertEqual(info["regime"], "chop")

    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=True)
    def test_fade_extended(self, _en):
        info = classify_intraday_regime(
            {
                "session_move_from_open_pct": 2.5,
                "pullback_from_high_pct": 0.4,
                "momentum_2h_pct": 0.2,
                "momentum_rth_today_pct": 2.0,
            }
        )
        self.assertEqual(info["regime"], "fade_extended")

    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=False)
    def test_disabled_neutral(self, _en):
        info = classify_intraday_regime({"momentum_rth_today_pct": 5.0})
        self.assertEqual(info["regime"], "neutral")
        self.assertFalse(info["enabled"])


class TestEntryGuard(unittest.TestCase):
    @patch("services.game5m_intraday_regime.intraday_regime_gate_mode", return_value="apply")
    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=True)
    def test_chop_blocks_weak_momentum_buy(self, _en, _gm):
        reasons: list = []
        decision, reasons, trig, prev, _ = apply_intraday_regime_entry_guard(
            "BUY",
            reasons,
            {"momentum_rth_today_pct": 1.3},
            technical_entry_branch="buy_rth_momentum",
            regime_info={"enabled": True, "regime": "chop", "reason": "chop"},
        )
        self.assertTrue(trig)
        self.assertEqual(decision, "HOLD")
        self.assertEqual(prev, "BUY")

    @patch("services.game5m_intraday_regime.intraday_regime_gate_mode", return_value="apply")
    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=True)
    def test_impulse_allows_momentum_buy(self, _en, _gm):
        reasons: list = []
        decision, _, trig, _, _ = apply_intraday_regime_entry_guard(
            "BUY",
            reasons,
            {"momentum_rth_today_pct": 3.0},
            technical_entry_branch="buy_rth_momentum",
            regime_info={"enabled": True, "regime": "impulse_up", "reason": "impulse"},
        )
        self.assertFalse(trig)
        self.assertEqual(decision, "BUY")

    @patch("services.game5m_intraday_regime.intraday_regime_gate_mode", return_value="log_only")
    @patch("services.game5m_intraday_regime.intraday_regime_enabled", return_value=True)
    def test_log_only_keeps_buy(self, _en, _gm):
        decision, _, trig, _, _ = apply_intraday_regime_entry_guard(
            "BUY",
            [],
            {"momentum_rth_today_pct": 1.0},
            technical_entry_branch="buy_rth_momentum",
            regime_info={"enabled": True, "regime": "chop", "reason": "chop"},
        )
        self.assertTrue(trig)
        self.assertEqual(decision, "BUY")


class TestHelpers(unittest.TestCase):
    def test_regime_from_context(self):
        self.assertEqual(regime_label_from_context({"intraday_regime": {"regime": "chop"}}), "chop")
        self.assertEqual(regime_label_from_context({"intraday_regime": "impulse_up"}), "impulse_up")

    def test_exit_multipliers(self):
        chop = exit_multipliers_for_regime("chop")
        impulse = exit_multipliers_for_regime("impulse_up")
        self.assertLess(chop["take_cap_mult"], 1.0)
        self.assertGreater(impulse["momentum_factor_mult"], 1.0)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Tests for stale/chase entry guard and extended-session momentum logic."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd

from services.recommend_5m import (
    apply_entry_stale_chase_guard,
    decide_game5m_technical,
    get_decision_5m_rule_thresholds,
)


class TestEntryStaleChaseGuard(unittest.TestCase):
    def test_downgrades_buy_near_stale_high(self):
        features = {
            "session_move_from_open_pct": 4.0,
            "pullback_from_high_pct": 1.5,
            "bars_since_session_high": 9,
        }
        with patch.dict(
            os.environ,
            {
                "GAME_5M_ENTRY_STALE_CHASE_GUARD_ENABLED": "true",
                "GAME_5M_ENTRY_EXTENDED_SESSION_MOVE_PCT": "2.5",
                "GAME_5M_ENTRY_CHASE_MAX_PULLBACK_FROM_HIGH_PCT": "2.0",
                "GAME_5M_ENTRY_CHASE_MIN_BARS_SINCE_HIGH": "6",
            },
            clear=False,
        ):
            dec, reasons, triggered, prev, msg = apply_entry_stale_chase_guard("BUY", [], features)
        self.assertTrue(triggered)
        self.assertEqual(dec, "HOLD")
        self.assertEqual(prev, "BUY")
        self.assertIn("stale/chase", msg or "")

    def test_allows_fresh_breakout(self):
        features = {
            "session_move_from_open_pct": 1.5,
            "pullback_from_high_pct": 0.2,
            "bars_since_session_high": 0,
        }
        with patch.dict(os.environ, {"GAME_5M_ENTRY_STALE_CHASE_GUARD_ENABLED": "true"}, clear=False):
            dec, _, triggered, _, _ = apply_entry_stale_chase_guard("STRONG_BUY", [], features)
        self.assertFalse(triggered)
        self.assertEqual(dec, "STRONG_BUY")

    def test_extended_session_uses_short_momentum_for_buy(self):
        th = get_decision_5m_rule_thresholds()
        th = dict(th)
        th["momentum_buy_min"] = 1.2
        features = {
            "price": 456.0,
            "low_5d": 400.0,
            "rsi_5m": 36.0,
            "volatility_5m_pct": 0.5,
            "momentum_2h_pct": 2.2,
            "momentum_rth_today_pct": 2.2,
            "momentum_rth_today_bars": 50,
            "momentum_rth_today_window_min": 120,
            "session_move_from_open_pct": 4.0,
            "momentum_short_pct": 0.1,
            "momentum_short_bars": 6,
            "bars_since_session_high": 9,
            "pullback_from_high_pct": 1.5,
        }
        closes = pd.Series([430.0] * 40 + [456.0])
        with patch.dict(
            os.environ,
            {
                "GAME_5M_ENTRY_EXTENDED_SESSION_MOVE_PCT": "2.5",
                "GAME_5M_ENTRY_CHASE_MIN_BARS_SINCE_HIGH": "6",
            },
            clear=False,
        ):
            dec, reasons, branch, _ = decide_game5m_technical(
                ticker="CIEN",
                features=features,
                closes=closes,
                th=th,
                rsi_prev_values=[35.0, 34.0],
                decision_rule_params=th,
                min_session_bars=6,
                premarket_intraday_momentum_pct=None,
                early_use_premarket_mom=False,
            )
        self.assertEqual(dec, "HOLD")
        self.assertIsNone(branch)

    def test_fresh_breakout_still_buys(self):
        th = get_decision_5m_rule_thresholds()
        th = dict(th)
        th["momentum_buy_min"] = 0.5
        features = {
            "price": 444.0,
            "low_5d": 400.0,
            "rsi_5m": 30.0,
            "volatility_5m_pct": 0.5,
            "momentum_2h_pct": 1.6,
            "momentum_rth_today_pct": 1.6,
            "momentum_rth_today_bars": 30,
            "momentum_rth_today_window_min": 60,
            "session_move_from_open_pct": 1.6,
            "momentum_short_pct": 1.4,
            "momentum_short_bars": 6,
            "bars_since_session_high": 0,
            "pullback_from_high_pct": 0.1,
        }
        closes = pd.Series([436.0] * 30 + [444.0])
        dec, _, branch, _ = decide_game5m_technical(
            ticker="CIEN",
            features=features,
            closes=closes,
            th=th,
            rsi_prev_values=[32.0, 31.0],
            decision_rule_params=th,
            min_session_bars=6,
            premarket_intraday_momentum_pct=None,
            early_use_premarket_mom=False,
        )
        self.assertIn(dec, ("BUY", "STRONG_BUY"))
        self.assertIsNotNone(branch)


if __name__ == "__main__":
    unittest.main()

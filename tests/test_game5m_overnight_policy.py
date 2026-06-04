# -*- coding: utf-8 -*-
"""Unit tests for game5m_overnight_policy (no DB)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.game5m_overnight_policy import (
    should_block_new_buy_for_overnight,
    should_eod_flatten_position,
    should_premarket_auto_flat,
)


class TestEodFlatten(unittest.TestCase):
    def test_disabled(self):
        with patch.dict(os.environ, {"GAME_5M_EOD_FLATTEN_ENABLED": "false"}, clear=False):
            flat, _ = should_eod_flatten_position(
                d5={},
                market_session_ctx={"session_phase": "NEAR_CLOSE", "minutes_until_close": 10},
                current_decision="BUY",
                pnl_current_pct=-5.0,
            )
        self.assertFalse(flat)

    def test_always_flat_near_close(self):
        env = {
            "GAME_5M_EOD_FLATTEN_ENABLED": "true",
            "GAME_5M_EOD_FLATTEN_ALWAYS": "true",
            "GAME_5M_EOD_FLATTEN_MINUTES_BEFORE_CLOSE": "30",
        }
        with patch.dict(os.environ, env, clear=False):
            flat, detail = should_eod_flatten_position(
                d5={},
                market_session_ctx={"session_phase": "REGULAR", "minutes_until_close": 15},
                current_decision="STRONG_BUY",
                pnl_current_pct=2.0,
            )
        self.assertTrue(flat)
        self.assertEqual(detail, "overnight_eod_flat")


class TestBlockNewBuy(unittest.TestCase):
    def test_near_close(self):
        env = {"GAME_5M_BLOCK_NEW_BUY_NEAR_CLOSE_ENABLED": "true", "GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE": "60"}
        with patch.dict(os.environ, env, clear=False):
            block, reason = should_block_new_buy_for_overnight(
                {"multiday_lr_horizon_1d_pct_vs_spot": 1.0},
                {"minutes_until_close": 30},
            )
        self.assertTrue(block)
        self.assertIn("near_close", reason)


class TestPremarketFlat(unittest.TestCase):
    def test_gap_down(self):
        env = {
            "GAME_5M_PREMARKET_AUTO_FLAT_ENABLED": "true",
            "GAME_5M_PREMARKET_GAP_FLAT_PCT": "-2.0",
            "GAME_5M_PREMARKET_AUTO_FLAT_USE_MULTIDAY": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            flat, reason = should_premarket_auto_flat({}, premarket_gap_pct=-3.5)
        self.assertTrue(flat)
        self.assertIn("gap", reason)


if __name__ == "__main__":
    unittest.main()

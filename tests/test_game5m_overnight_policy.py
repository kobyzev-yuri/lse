# -*- coding: utf-8 -*-
"""Unit tests for game5m_overnight_policy (no DB)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.game5m_overnight_policy import (
    evaluate_premarket_flat_decision,
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

    def test_selective_no_flat_on_bullish_multiday(self):
        env = {
            "GAME_5M_EOD_FLATTEN_ENABLED": "true",
            "GAME_5M_EOD_FLATTEN_ALWAYS": "false",
            "GAME_5M_EOD_FLATTEN_MINUTES_BEFORE_CLOSE": "30",
            "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE": "apply",
            "GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
            "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.20",
            "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.4,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.3,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.2,
        }
        with patch.dict(os.environ, env, clear=False):
            flat, detail = should_eod_flatten_position(
                d5=d5,
                market_session_ctx={"session_phase": "REGULAR", "minutes_until_close": 15},
                current_decision="BUY",
                pnl_current_pct=0.5,
            )
        self.assertFalse(flat)
        self.assertEqual(detail, "")

    def test_bullish_multiday_holds_despite_moderate_loss(self):
        """SNDK-like: бычий multiday, PnL −0.9% — не flat по EOD (deep stop −4%)."""
        env = {
            "GAME_5M_EOD_FLATTEN_ENABLED": "true",
            "GAME_5M_EOD_FLATTEN_ALWAYS": "false",
            "GAME_5M_EOD_FLATTEN_MINUTES_BEFORE_CLOSE": "30",
            "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE": "apply",
            "GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
            "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.20",
            "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
            "GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT": "-4.0",
            "GAME_5M_EOD_FLATTEN_MAX_LOSS_TO_FORCE_PCT": "-0.5",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.88,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.91,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.53,
        }
        with patch.dict(os.environ, env, clear=False):
            flat, detail = should_eod_flatten_position(
                d5=d5,
                market_session_ctx={"session_phase": "REGULAR", "minutes_until_close": 15},
                current_decision="BUY",
                pnl_current_pct=-0.9,
            )
        self.assertFalse(flat, msg=detail)

    def test_bullish_multiday_deep_loss_still_flats(self):
        env = {
            "GAME_5M_EOD_FLATTEN_ENABLED": "true",
            "GAME_5M_EOD_FLATTEN_ALWAYS": "false",
            "GAME_5M_EOD_FLATTEN_MINUTES_BEFORE_CLOSE": "30",
            "GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
            "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.20",
            "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
            "GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT": "-4.0",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.4,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.3,
        }
        with patch.dict(os.environ, env, clear=False):
            flat, detail = should_eod_flatten_position(
                d5=d5,
                market_session_ctx={"session_phase": "REGULAR", "minutes_until_close": 15},
                current_decision="BUY",
                pnl_current_pct=-4.5,
            )
        self.assertTrue(flat)
        self.assertEqual(detail, "overnight_eod_flat_loss_deep")


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
            "GAME_5M_PREMARKET_FLAT_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "false",
            "GAME_5M_PREMARKET_FLAT_HOLD_ON_GAP_REVERSAL_REGIME": "false",
            "GAME_5M_PREMARKET_RECOVERY_ML_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            flat, reason = should_premarket_auto_flat({}, premarket_gap_pct=-3.5)
        self.assertTrue(flat)
        self.assertIn("gap", reason)

    def test_asml_like_gap_hold_bullish_multiday_and_reversal_regime(self):
        """Gap −2.01% при бычьем multiday и gap_reversal_opportunity — не flat в premarket."""
        env = {
            "GAME_5M_PREMARKET_AUTO_FLAT_ENABLED": "true",
            "GAME_5M_PREMARKET_GAP_FLAT_PCT": "-2.0",
            "GAME_5M_PREMARKET_AUTO_FLAT_USE_MULTIDAY": "true",
            "GAME_5M_PREMARKET_FLAT_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
            "GAME_5M_PREMARKET_FLAT_HOLD_ON_GAP_REVERSAL_REGIME": "true",
            "GAME_5M_PREMARKET_RECOVERY_ML_ENABLED": "false",
            "GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
            "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.20",
            "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
            "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE": "apply",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.05,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.34,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.46,
            "premarket_gap_pct": -2.01,
            "forecast_layer": {"regime": "gap_reversal_opportunity"},
        }
        with patch.dict(os.environ, env, clear=False):
            ev = evaluate_premarket_flat_decision(d5, premarket_gap_pct=-2.01, pnl_current_pct=-2.33)
        self.assertFalse(ev["should_flat"])
        hold = " ".join(ev.get("hold_reasons") or [])
        self.assertIn("bullish_multiday", hold)
        self.assertIn("gap_reversal_opportunity", hold)

    def test_bearish_multiday_still_flats(self):
        env = {
            "GAME_5M_PREMARKET_AUTO_FLAT_ENABLED": "true",
            "GAME_5M_PREMARKET_AUTO_FLAT_USE_GAP": "false",
            "GAME_5M_PREMARKET_AUTO_FLAT_USE_MULTIDAY": "true",
            "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE": "apply",
            "GAME_5M_PREMARKET_FLAT_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": -0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": -0.4,
            "multiday_lr_horizon_3d_pct_vs_spot": -0.3,
        }
        with patch.dict(os.environ, env, clear=False):
            flat, _ = should_premarket_auto_flat(d5, premarket_gap_pct=-0.5)
        self.assertTrue(flat)

    def test_deep_loss_overrides_hold(self):
        env = {
            "GAME_5M_PREMARKET_AUTO_FLAT_ENABLED": "true",
            "GAME_5M_PREMARKET_GAP_FLAT_PCT": "-2.0",
            "GAME_5M_PREMARKET_FLAT_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
            "GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT": "-4.0",
            "GAME_5M_PREMARKET_RECOVERY_ML_ENABLED": "false",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.4,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.3,
            "forecast_layer": {"regime": "gap_reversal_opportunity"},
        }
        with patch.dict(os.environ, env, clear=False):
            ev = evaluate_premarket_flat_decision(d5, premarket_gap_pct=-5.0, pnl_current_pct=-4.5)
        self.assertTrue(ev["should_flat"])
        self.assertEqual(ev.get("skipped_hold"), "deep_loss")


if __name__ == "__main__":
    unittest.main()

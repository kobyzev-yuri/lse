# -*- coding: utf-8 -*-
"""Unit tests for multiday_lr_gate (no DB)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.multiday_lr_gate import (
    evaluate_multiday_entry_gate,
    evaluate_multiday_hold_gate,
    evaluate_multiday_overnight_gate,
    finalize_technical_decision_with_multiday,
    should_skip_early_exit_for_bullish_multiday,
)


class TestMultidayEntryGate(unittest.TestCase):
    def test_none_mode_skips(self):
        with patch.dict(os.environ, {"GAME_5M_MULTIDAY_ENTRY_GATE_MODE": "none"}, clear=False):
            g = evaluate_multiday_entry_gate({"multiday_lr_horizon_1d_pct_vs_spot": -1.0})
        self.assertEqual(g["mode"], "none")
        self.assertFalse(g["would_hold"])

    def test_bearish_1d_would_hold(self):
        env = {
            "GAME_5M_MULTIDAY_ENTRY_GATE_MODE": "log_only",
            "GAME_5M_MULTIDAY_ENTRY_TAU_1D_PCT": "0.25",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": -0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.1,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.1,
        }
        with patch.dict(os.environ, env, clear=False):
            g = evaluate_multiday_entry_gate(d5)
        self.assertTrue(g["would_hold"])
        self.assertEqual(g["status"], "ok")

    def test_apply_lowers_effective(self):
        env = {"GAME_5M_MULTIDAY_ENTRY_GATE_MODE": "apply", "GAME_5M_MULTIDAY_ENTRY_TAU_1D_PCT": "0.1"}
        out = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "multiday_lr_horizon_1d_pct_vs_spot": -0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.0,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.0,
        }
        with patch.dict(os.environ, env, clear=False):
            finalize_technical_decision_with_multiday(out)
        self.assertEqual(out["technical_decision_effective"], "HOLD")
        self.assertTrue(out["multiday_lr_entry_gate_applied"])


class TestMultidayOvernightGate(unittest.TestCase):
    def test_bearish_avoid_overnight(self):
        env = {
            "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE": "log_only",
            "GAME_5M_MULTIDAY_OVERNIGHT_TAU_1D_PCT": "0.1",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": -0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.0,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.0,
        }
        with patch.dict(os.environ, env, clear=False):
            g = evaluate_multiday_overnight_gate(d5)
        self.assertTrue(g["would_avoid_overnight"])


class TestMultidayHoldGate(unittest.TestCase):
    def test_bullish_would_defer(self):
        env = {
            "GAME_5M_MULTIDAY_HOLD_GATE_MODE": "log_only",
            "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.15",
            "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
            "GAME_5M_MULTIDAY_HOLD_EXIT_DETAILS": "early_derisk",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.3,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.25,
            "multiday_lr_horizon_3d_pct_vs_spot": -0.1,
        }
        with patch.dict(os.environ, env, clear=False):
            g = evaluate_multiday_hold_gate(d5, exit_detail="early_derisk", pnl_current_pct=-1.0)
        self.assertTrue(g["would_defer_exit"])

    def test_wrong_exit_detail_skips(self):
        env = {"GAME_5M_MULTIDAY_HOLD_GATE_MODE": "log_only", "GAME_5M_MULTIDAY_HOLD_EXIT_DETAILS": "early_derisk"}
        d5 = {"multiday_lr_horizon_1d_pct_vs_spot": 1.0, "multiday_lr_horizon_2d_pct_vs_spot": 1.0}
        with patch.dict(os.environ, env, clear=False):
            g = evaluate_multiday_hold_gate(d5, exit_detail="stale_reversal")
        self.assertIn("exit_detail_not_allowed", g.get("skip_reason", ""))

    def test_skip_early_exit_apply_bullish(self):
        env = {
            "GAME_5M_MULTIDAY_HOLD_GATE_MODE": "apply",
            "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.20",
            "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
            "GAME_5M_MULTIDAY_HOLD_EXIT_DETAILS": "early_derisk,stale_reversal",
        }
        d5 = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.5,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.4,
            "multiday_lr_horizon_3d_pct_vs_spot": 0.1,
        }
        with patch.dict(os.environ, env, clear=False):
            skip, gate = should_skip_early_exit_for_bullish_multiday(
                d5, exit_detail="early_derisk", pnl_current_pct=-1.5
            )
        self.assertTrue(skip)
        self.assertTrue(gate.get("would_defer_exit"))


if __name__ == "__main__":
    unittest.main()

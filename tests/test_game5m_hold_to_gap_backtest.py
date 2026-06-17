# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services.game5m_hold_to_gap_backtest import (
    _bullish_horizons,
    _horizons_from_ctx,
    _next_session_opens,
    _simulate_current_eod_would_flatten,
    build_hold_to_gap_backtest,
)


class TestHoldToGapBacktest(unittest.TestCase):
    def test_bullish_horizons(self):
        self.assertTrue(_bullish_horizons({"1d": 0.5, "2d": 0.3, "3d": -0.1}))
        self.assertFalse(_bullish_horizons({"1d": 0.1, "2d": 0.1, "3d": -0.5}))

    def test_horizons_from_ctx_flat_fields(self):
        ctx = {
            "multiday_lr_horizon_1d_pct_vs_spot": 0.42,
            "multiday_lr_horizon_2d_pct_vs_spot": 0.55,
        }
        h = _horizons_from_ctx(ctx)
        self.assertEqual(h["1d"], 0.42)
        self.assertEqual(h["2d"], 0.55)

    def test_next_session_opens(self):
        ny = ZoneInfo("America/New_York")
        rows = []
        for day in ("2026-06-10", "2026-06-11"):
            ts = datetime.fromisoformat(f"{day}T09:35:00").replace(tzinfo=ny)
            rows.append({"datetime": ts, "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5})
            ts2 = datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=ny)
            rows.append({"datetime": ts2, "Open": 100.5, "High": 102.0, "Low": 100.0, "Close": 101.5})
        df = pd.DataFrame(rows)
        exit_ts = datetime.fromisoformat("2026-06-10T15:55:00").replace(tzinfo=ny)
        opens = _next_session_opens(df, exit_ts)
        self.assertEqual(opens.get("d1_open"), 100.0)

    def test_simulate_eod_bullish_hold(self):
        flat, reason = _simulate_current_eod_would_flatten(
            {"1d": 0.6, "2d": 0.5, "3d": 0.4},
            current_decision="BUY",
            pnl_current_pct=0.5,
            always_flat=False,
        )
        self.assertFalse(flat)
        self.assertIn("bullish", reason)

    def test_build_backtest_empty(self):
        out = build_hold_to_gap_backtest([], [], {}, engine=None, limit=10)
        self.assertEqual(out["mode"], "hold_to_gap_counterfactual")
        self.assertEqual(out["trades_analyzed"], 0)


if __name__ == "__main__":
    unittest.main()

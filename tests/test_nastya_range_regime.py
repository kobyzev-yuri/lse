"""Tests for nastya range-regime helpers (no network)."""

import unittest

import pandas as pd

from services.nastya_range_regime import (
    REPORT_SCHEMA_VERSION,
    _blend_band,
    _detect_local,
    _excel_anchors,
    build_nastya_llm_prompts,
)


class TestNastyaRangeRegime(unittest.TestCase):
    def test_detect_local_transition(self):
        idx = pd.date_range("2026-01-01", periods=80, freq="B")
        close = pd.Series(range(80), index=idx, dtype=float) + 100
        high = close + 1
        low = close - 1
        out = _detect_local(close, high, low)
        self.assertIn(out["regime"], {"uptrend", "transition", "range", "downtrend"})
        self.assertIsNotNone(out["channel_lo_20d"])

    def test_blend_bias_near_floor(self):
        local = {"regime": "transition", "channel_lo_20d": 90.0, "channel_hi_20d": 120.0}
        xa = {"drop20_from_52w": 95.0, "target_17pct": 130.0}
        band = _blend_band(96.0, local, xa)
        self.assertEqual(band["bias_exit"], "up")

    def test_excel_missing(self):
        ex = pd.DataFrame()
        self.assertFalse(_excel_anchors(ex, "ARM").get("in_excel"))

    def test_schema_version_bumped_for_trend(self):
        self.assertGreaterEqual(REPORT_SCHEMA_VERSION, 3)

    def test_llm_prompts_cover_nastya_goals(self):
        row = {
            "ticker": "META",
            "status": "ok",
            "regime": "transition",
            "bias_exit": "up",
            "band_floor": 637,
            "band_ceiling": 686,
            "pos_in_band": 0.18,
            "rvol_20": 1.06,
            "rvol_flag": "normal",
            "approx_range_age_days": 75,
            "portfolio_trend_regime": "neutral",
            "portfolio_trend_ret_20d_pct": -2.1,
        }
        system, user = build_nastya_llm_prompts(
            row,
            market={"vix_regime": "calm"},
            portfolio_slim={"in_portfolio_game": True, "decision": "HOLD"},
        )
        self.assertIn("Пол и потолок", system)
        self.assertIn("Боковик", system)
        self.assertIn("RVOL", system)
        self.assertIn("ML portfolio", system)
        self.assertIn("META", user)
        self.assertIn("neutral", user)


if __name__ == "__main__":
    unittest.main()

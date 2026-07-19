"""Tests for nastya range-regime helpers (no network)."""

import unittest
from unittest.mock import patch

import pandas as pd

from services.nastya_range_regime import _blend_band, _detect_local, _excel_anchors


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


if __name__ == "__main__":
    unittest.main()

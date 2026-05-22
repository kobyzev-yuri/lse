# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from services.game5m_forecast_layer import build_game5m_forecast_envelope


class TestGame5mForecastLayer(unittest.TestCase):
    def test_gap_fade_regime(self):
        env = build_game5m_forecast_envelope(
            {
                "ticker_open_gap_predicted_pct": 1.4,
                "ticker_open_gap_predicted_source": "pooled_ridge_v1",
                "multiday_lr_horizon_1d_pct_vs_spot": -0.4,
                "multiday_lr_horizon_2d_pct_vs_spot": -0.2,
            }
        )
        self.assertTrue(env["ready"])
        self.assertEqual(env["regime"], "gap_fade_risk")
        self.assertEqual(env["open_gap"]["source"], "pooled_ridge_v1")

    def test_neutral_when_no_forecast(self):
        env = build_game5m_forecast_envelope({})
        self.assertFalse(env["ready"])
        self.assertEqual(env["regime"], "neutral_or_unavailable")


if __name__ == "__main__":
    unittest.main()

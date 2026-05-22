# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.decision_stack.game5m import collect_game5m_contributions


class TestDecisionStackForecastLayer(unittest.TestCase):
    def test_forecast_layer_contribution_log_only(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "forecast_layer": {
                "ready": True,
                "regime": "gap_fade_risk",
                "open_gap": {"predicted_pct": 1.2, "confidence": 0.7},
                "horizons_pct": {"1d": -0.3, "2d": -0.2},
            },
        }
        with patch.dict(os.environ, {"DECISION_STACK_FORECAST_GATE_MODE": "log_only"}, clear=False):
            cs = collect_game5m_contributions(d5, ticker="MU")
        fl = [c for c in cs if c.get("contour_id") == "forecast_layer"]
        self.assertEqual(len(fl), 1)
        self.assertEqual(fl[0]["action"], "telemetry")
        self.assertTrue(fl[0]["metrics"]["would_downgrade"])

    def test_forecast_layer_gap_up_opportunity_shadow_boost(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "forecast_layer": {
                "ready": True,
                "regime": "aligned_bullish",
                "open_gap": {"predicted_pct": 1.5, "confidence": 0.7},
                "horizons_pct": {"1d": 0.25, "2d": 0.2},
                "gap_up_opportunity": {
                    "candidate": True,
                    "gap_pct": 2.2,
                    "source": "premarket_gap",
                    "should_boost_entry": True,
                    "reason": "gap_up_confirmed",
                },
            },
        }
        with patch.dict(os.environ, {"DECISION_STACK_FORECAST_GATE_MODE": "log_only"}, clear=False):
            cs = collect_game5m_contributions(d5, ticker="ASML")
        fl = [c for c in cs if c.get("contour_id") == "forecast_layer"]
        self.assertEqual(len(fl), 1)
        self.assertEqual(fl[0]["action"], "telemetry")
        self.assertTrue(fl[0]["metrics"]["would_boost_entry"])
        self.assertGreater(fl[0]["strength"], 0)


if __name__ == "__main__":
    unittest.main()

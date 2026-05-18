# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.analyzer_ml_arbiter import build_game5m_gap_forecast_arbiter
from services.game5m_gap_forecast import pool_gap_forecast_metrics


class TestGapForecastPool(unittest.TestCase):
    def test_pool_sector_complete(self):
        rows = [
            {
                "symbol": "SMH",
                "pred_sector_gap_pct": 0.5,
                "open_gap_pct": 0.3,
                "error_pred_vs_open_pct": -0.2,
                "error_premarket_vs_open_pct": 0.1,
            },
            {
                "symbol": "SMH",
                "pred_sector_gap_pct": 0.4,
                "open_gap_pct": -0.1,
                "error_pred_vs_open_pct": -0.5,
                "error_premarket_vs_open_pct": None,
            },
        ]
        p = pool_gap_forecast_metrics(rows, sector_proxy="SMH")
        self.assertEqual(p["sector"]["n_complete"], 2)
        self.assertIsNotNone(p["sector"]["sign_agreement_rate"])


class TestGapForecastArbiter(unittest.TestCase):
    def test_insufficient_when_no_rows(self):
        env = {"GAME_5M_GAP_FORECAST_LOG_ENABLED": "true"}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "services.game5m_gap_forecast.fetch_gap_forecast_rows",
                return_value=[],
            ):
                with patch("services.game5m_gap_forecast.ensure_gap_forecast_table"):
                    out = build_game5m_gap_forecast_arbiter(
                        {},
                        strategy="GAME_5M",
                        engine=object(),
                        days=60,
                    )
        self.assertEqual(out["mode"], "ok")
        self.assertEqual(out["overall_verdict"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()

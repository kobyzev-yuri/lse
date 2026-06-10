# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from services.premarket_chart import build_premarket_table_rows


class TestPremarketChartLiveForecast(unittest.TestCase):
    @patch("services.premarket_chart.is_preopen_live", return_value=True)
    @patch("services.premarket_chart._macro_risk_cached", return_value={"macro_predicted_sector_gap_pct": -3.0})
    @patch("services.premarket_chart._minutes_until_open_global", return_value=42)
    @patch("services.premarket_chart._rows_from_gap_forecast_db")
    @patch("services.premarket_chart._yahoo_context_row")
    @patch("services.premarket_open_gap_forecast.build_open_gap_forecast_fields")
    def test_live_premarket_enriches_forecasts(self, fc_mock, yahoo_mock, db_mock, *_args):
        db_mock.return_value = {
            "SNDK": {
                "ticker": "SNDK",
                "prev_close": 100.0,
                "premarket_last": 94.0,
                "premarket_gap_pct": -6.0,
                "pred_ticker_gap_pct": 0.4,
                "pred_sector_gap_pct": -3.0,
                "open_gap_pct": None,
                "source": "db",
                "ml_db_snapshot_ts": "2026-06-10 08:30",
            }
        }
        yahoo_mock.return_value = {
            "ticker": "SNDK",
            "prev_close": 100.0,
            "premarket_last": 93.5,
            "premarket_gap_pct": -6.5,
            "minutes_until_open": 40,
            "premarket_last_time_et": "2026-06-10 08:45",
        }
        fc_mock.return_value = {
            "baseline_open_gap_pct": -6.5,
            "ml_open_gap_pct": -5.8,
            "effective_open_gap_pct": -6.5,
            "effective_open_gap_source": "premarket_baseline",
        }
        rows = build_premarket_table_rows(["SNDK"], yahoo_fallback=False)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["premarket_gap_pct"], -6.5)
        self.assertEqual(r["premarket_price_source"], "yahoo_live")
        self.assertEqual(r["baseline_open_gap_pct"], -6.5)
        self.assertEqual(r["effective_open_gap_pct"], -6.5)
        fc_mock.assert_called_once()
        call_kw = fc_mock.call_args.kwargs
        self.assertAlmostEqual(call_kw["premarket_gap_pct"], -6.5)


    @patch("services.premarket_chart.is_preopen_live", return_value=False)
    @patch("services.premarket_chart._macro_risk_cached", return_value=None)
    @patch("services.premarket_chart._minutes_until_open_global", return_value=0)
    @patch("services.premarket_chart._rows_from_gap_forecast_db")
    @patch("services.premarket_open_gap_forecast.build_open_gap_forecast_fields")
    def test_forecasts_when_open_gap_already_in_db(self, fc_mock, db_mock, *_args):
        """После open прогнозы всё равно считаются (baseline/ML/effective не пустые)."""
        db_mock.return_value = {
            "SNDK": {
                "ticker": "SNDK",
                "prev_close": 100.0,
                "premarket_last": 94.0,
                "premarket_gap_pct": -6.0,
                "pred_ticker_gap_pct": 0.4,
                "open_gap_pct": -5.08,
                "error_pred_ticker_vs_open_pct": -5.48,
                "source": "db",
            }
        }
        fc_mock.return_value = {
            "baseline_open_gap_pct": -6.0,
            "ml_open_gap_pct": -5.2,
            "effective_open_gap_pct": -6.0,
            "effective_open_gap_source": "premarket_baseline",
        }
        rows = build_premarket_table_rows(["SNDK"], yahoo_fallback=False)
        r = rows[0]
        self.assertEqual(r["baseline_open_gap_pct"], -6.0)
        self.assertEqual(r["effective_open_gap_pct"], -6.0)
        self.assertEqual(r["open_gap_pct"], -5.08)
        self.assertAlmostEqual(r["error_pred_ticker_vs_open_pct"], -5.08 - (-5.2), places=3)
        fc_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

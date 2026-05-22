# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from services.premarket_chart import _rows_from_gap_forecast_db


class TestPremarketChartGapError(unittest.TestCase):
    def test_error_recomputed_from_current_pred_and_open(self):
        df = pd.DataFrame(
            [
                {
                    "symbol": "ASML",
                    "prev_close": 100.0,
                    "premarket_last": 101.0,
                    "premarket_gap_pct": 1.0,
                    "pred_sector_gap_pct": 0.5,
                    "pred_ticker_gap_pct": 0.48,
                    "pred_ticker_source": "ticker_ols_v2",
                    "pred_ticker_model_version": "v2",
                    "rth_open_price": 103.66,
                    "open_gap_pct": 3.66,
                    "source_open": "rth_5m",
                    "error_pred_ticker_vs_open_pct": 2.38,
                    "snapshot_ts_premarket": pd.Timestamp("2026-05-22T13:53:00Z"),
                }
            ]
        )
        fake_engine = MagicMock()
        fake_conn = MagicMock()
        fake_engine.connect.return_value.__enter__.return_value = fake_conn
        with patch("config_loader.get_database_url", return_value="postgresql://test"), patch(
            "sqlalchemy.create_engine", return_value=fake_engine
        ), patch("pandas.read_sql", return_value=df):
            rows = _rows_from_gap_forecast_db(["ASML"])
        self.assertAlmostEqual(rows["ASML"]["error_pred_ticker_vs_open_pct"], 3.18)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from services.game5m_gap_forecast import pool_gap_forecast_metrics
from services.game5m_gap_forecast import UPSERT_PREMARKET_SQL


class TestTickerGapPool(unittest.TestCase):
    def test_premarket_upsert_does_not_overwrite_after_open(self):
        self.assertIn("WHERE game5m_gap_forecast_daily.open_gap_pct IS NULL", UPSERT_PREMARKET_SQL)

    def test_post_open_snapshots_excluded_from_metrics(self):
        rows = [
            {
                "symbol": "SNDK",
                "snapshot_ts_premarket": datetime(2026, 5, 22, 13, 20, tzinfo=timezone.utc),
                "pred_ticker_gap_pct": 0.8,
                "open_gap_pct": 1.0,
                "error_pred_ticker_vs_open_pct": 0.2,
            },
            {
                "symbol": "MU",
                "snapshot_ts_premarket": datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
                "pred_ticker_gap_pct": 0.8,
                "open_gap_pct": 1.0,
                "error_pred_ticker_vs_open_pct": 0.2,
            },
        ]
        p = pool_gap_forecast_metrics(rows, sector_proxy="SMH")
        self.assertEqual(p["ticker_v2"]["n_complete"], 1)
        self.assertEqual(p["n_invalid_post_open_snapshots"], 1)

    def test_ticker_v2_vs_sector_baseline(self):
        rows = [
            {
                "symbol": "SNDK",
                "pred_sector_gap_pct": 0.3,
                "pred_ticker_gap_pct": 0.8,
                "open_gap_pct": 1.0,
                "error_pred_vs_open_pct": 0.7,
                "error_pred_ticker_vs_open_pct": 0.2,
            },
            {
                "symbol": "NVDA",
                "pred_sector_gap_pct": 0.3,
                "pred_ticker_gap_pct": 0.5,
                "open_gap_pct": -0.5,
                "error_pred_vs_open_pct": -0.8,
                "error_pred_ticker_vs_open_pct": -1.0,
            },
        ]
        p = pool_gap_forecast_metrics(rows, sector_proxy="SMH")
        self.assertEqual(p["ticker_v2"]["n_complete"], 2)
        self.assertLess(
            float(p["ticker_v2"]["mean_abs_error_pred_pp"]),
            float(p["game_sector_baseline"]["mean_abs_error_pred_pp"]),
        )
        self.assertIsNotNone(p.get("ticker_vs_sector_mae_delta_pp"))


if __name__ == "__main__":
    unittest.main()

"""Rolling gap forecast metrics and effective-source policy."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from services.game5m_gap_forecast import (
    _ml_beats_baseline_mae,
    pool_gap_forecast_metrics,
    pool_gap_forecast_metrics_windows,
)
from services.premarket_open_gap_forecast import ml_beats_baseline_on_metrics, pick_effective_open_gap_pct


def _row(td: date, sym: str, pm: float, ml: float, op: float) -> dict:
    return {
        "trade_date": td,
        "symbol": sym,
        "premarket_gap_pct": pm,
        "pred_ticker_gap_pct": ml,
        "pred_sector_gap_pct": 0.1,
        "open_gap_pct": op,
        "error_premarket_vs_open_pct": op - pm,
        "error_pred_ticker_vs_open_pct": op - ml,
        "snapshot_ts_premarket": "2026-06-01T12:00:00Z",
        "open_filled_ts": None,
    }


class TestGapRollingMetrics(unittest.TestCase):
    def test_ml_beats_baseline_mae(self):
        pooled = {
            "ticker_v2": {"mean_abs_error_pred_pp": 1.0},
            "premarket_baseline": {"mean_abs_error_pred_pp": 1.5},
        }
        self.assertTrue(_ml_beats_baseline_mae(pooled))
        pooled["ticker_v2"]["mean_abs_error_pred_pp"] = 2.0
        self.assertFalse(_ml_beats_baseline_mae(pooled))

    def test_rolling_windows(self):
        d0 = date(2026, 6, 11)
        rows = [
            _row(d0 - timedelta(days=i), "MU", 1.0, 1.2, 1.1)
            for i in range(20)
        ]
        rolling = pool_gap_forecast_metrics_windows(rows, sector_proxy="SMH", windows=(14,))
        self.assertIn("14", rolling)
        self.assertIsNotNone(rolling["14"].get("premarket_mae_pp"))

    def test_policy_prefers_baseline_when_ml_not_beating(self):
        metrics = {
            "rolling": {
                "14": {"ml_beats_baseline_mae": False},
                "30": {"ml_beats_baseline_mae": False},
            },
            "pooled": {
                "ticker_v2": {"mean_abs_error_pred_pp": 2.0},
                "premarket_baseline": {"mean_abs_error_pred_pp": 1.0},
            },
        }
        self.assertFalse(ml_beats_baseline_on_metrics(metrics))
        eff, src, _ = pick_effective_open_gap_pct(
            baseline_open_gap_pct=1.0,
            ml_open_gap_pct=1.5,
            policy="auto",
            metrics=metrics,
        )
        self.assertEqual(src, "premarket_baseline")
        self.assertEqual(eff, 1.0)

    def test_policy_picks_ml_when_both_windows_beat(self):
        metrics = {
            "rolling": {
                "14": {"ml_beats_baseline_mae": True},
                "30": {"ml_beats_baseline_mae": True},
            },
            "pooled": {
                "ticker_v2": {"mean_abs_error_pred_pp": 0.8},
                "premarket_baseline": {"mean_abs_error_pred_pp": 1.2},
            },
        }
        self.assertTrue(ml_beats_baseline_on_metrics(metrics))
        eff, src, _ = pick_effective_open_gap_pct(
            baseline_open_gap_pct=1.0,
            ml_open_gap_pct=1.2,
            policy="auto",
            metrics=metrics,
        )
        self.assertEqual(src, "ml_open_gap")
        self.assertEqual(eff, 1.2)


if __name__ == "__main__":
    unittest.main()

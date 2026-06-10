# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from services.premarket_open_gap_forecast import (
    build_open_gap_forecast_fields,
    ml_beats_baseline_on_metrics,
    pick_effective_open_gap_pct,
)


class TestPremarketOpenGapForecast(unittest.TestCase):
    def test_pick_auto_prefers_baseline_when_ml_worse(self):
        metrics = {
            "ticker_v2": {"mean_abs_error_pred_pp": 1.62},
            "premarket_baseline": {"mean_abs_error_pred_pp": 1.36},
        }
        eff, src, pol = pick_effective_open_gap_pct(
            baseline_open_gap_pct=-5.7,
            ml_open_gap_pct=0.4,
            policy="auto",
            metrics=metrics,
        )
        self.assertEqual(eff, -5.7)
        self.assertEqual(src, "premarket_baseline")
        self.assertEqual(pol, "auto")
        self.assertFalse(ml_beats_baseline_on_metrics(metrics))

    def test_pick_auto_uses_ml_when_better(self):
        metrics = {
            "ticker_v2": {"mean_abs_error_pred_pp": 1.1},
            "premarket_baseline": {"mean_abs_error_pred_pp": 1.36},
        }
        eff, src, _ = pick_effective_open_gap_pct(
            baseline_open_gap_pct=-5.7,
            ml_open_gap_pct=-4.2,
            policy="auto",
            metrics=metrics,
        )
        self.assertEqual(eff, -4.2)
        self.assertEqual(src, "ml_open_gap")

    @patch("services.premarket_open_gap_forecast.load_gap_forecast_metrics")
    @patch("services.ticker_open_gap_predict.predict_ticker_open_gap_detail")
    def test_build_fields_includes_baseline_and_effective(self, pred_mock, metrics_mock):
        metrics_mock.return_value = {
            "ticker_v2": {"mean_abs_error_pred_pp": 1.62},
            "premarket_baseline": {"mean_abs_error_pred_pp": 1.36},
        }
        pred_mock.return_value = {
            "predicted_pct": 0.4,
            "source": "pooled_ridge_v2",
            "model_version": "pooled_ridge_v2",
        }
        out = build_open_gap_forecast_fields("SNDK", premarket_gap_pct=-5.69)
        self.assertEqual(out["baseline_open_gap_pct"], -5.69)
        self.assertEqual(out["ml_open_gap_pct"], 0.4)
        self.assertEqual(out["effective_open_gap_pct"], -5.69)
        self.assertEqual(out["effective_open_gap_source"], "premarket_baseline")


if __name__ == "__main__":
    unittest.main()

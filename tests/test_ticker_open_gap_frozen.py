# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from services.ticker_open_gap_predict import (
    _use_frozen_gap_snapshot,
    attach_ticker_open_gap_fields,
    resolve_ticker_open_gap_fact,
)


class TestTickerOpenGapFrozen(unittest.TestCase):
    def test_use_frozen_after_open(self):
        frozen = {"pred_ticker_gap_pct": 2.1, "open_gap_pct": 3.0}
        self.assertTrue(_use_frozen_gap_snapshot("REGULAR", frozen))
        self.assertFalse(_use_frozen_gap_snapshot("PRE_MARKET", {"pred_ticker_gap_pct": 2.1}))

    def test_use_frozen_premarket_without_open(self):
        frozen = {"pred_ticker_gap_pct": 1.0, "open_gap_pct": None}
        self.assertTrue(_use_frozen_gap_snapshot("REGULAR", frozen))
        self.assertTrue(_use_frozen_gap_snapshot("PRE_MARKET", frozen) is False)

    def test_resolve_fact_prefers_db_open(self):
        fact, basis = resolve_ticker_open_gap_fact(
            {"rth_open_gap_pct": 7.0},
            frozen={"open_gap_pct": -0.31},
        )
        self.assertEqual(fact, -0.31)
        self.assertEqual(basis, "open_db")

    @patch("services.ticker_open_gap_predict.predict_ticker_open_gap_detail")
    @patch("services.game5m_gap_forecast.load_frozen_gap_snapshot")
    def test_attach_uses_frozen_pred_in_regular(self, mock_load, mock_predict):
        mock_load.return_value = {
            "pred_ticker_gap_pct": 2.14,
            "pred_ticker_source": "ticker_ols_v2_premarket_blend",
            "pred_ticker_model_version": "v2_sector_premarket_blend",
            "premarket_gap_pct": 4.5,
            "open_gap_pct": 3.38,
        }
        out = {"session_phase": "REGULAR"}
        attach_ticker_open_gap_fields(out, ticker="MU")
        mock_predict.assert_not_called()
        self.assertEqual(out["ticker_open_gap_predicted_pct"], 2.14)
        self.assertEqual(out["ticker_open_gap_predicted_source"], "ticker_ols_v2_premarket_blend")
        self.assertEqual(out["premarket_gap_pct"], 4.5)
        self.assertEqual(out["ticker_open_gap_fact_pct"], 3.38)
        self.assertEqual(out["ticker_open_gap_observable_baseline_pct"], 4.5)

    @patch("services.ticker_open_gap_predict.predict_ticker_open_gap_detail")
    @patch("services.game5m_gap_forecast.load_frozen_gap_snapshot")
    def test_attach_live_predict_in_premarket(self, mock_load, mock_predict):
        mock_load.return_value = {
            "pred_ticker_gap_pct": 1.0,
            "pred_ticker_source": "ticker_ols_v2",
            "open_gap_pct": None,
            "premarket_gap_pct": 1.2,
        }
        mock_predict.return_value = {
            "predicted_pct": 0.9,
            "source": "pooled_ridge_v1",
        }
        out = {"session_phase": "PRE_MARKET", "premarket_gap_pct": 1.2}
        attach_ticker_open_gap_fields(out, ticker="MU")
        mock_predict.assert_called_once()
        self.assertEqual(out["ticker_open_gap_predicted_pct"], 0.9)


if __name__ == "__main__":
    unittest.main()

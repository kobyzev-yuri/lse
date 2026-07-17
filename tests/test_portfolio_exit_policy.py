"""Portfolio exit policy: ML take clamp and trailing."""

import unittest
from unittest.mock import MagicMock, patch

from services.portfolio_exit_policy import (
    clamp_take_pct,
    compute_ml_adjusted_take,
    evaluate_portfolio_exit,
    resolve_exit_regime,
    trailing_take_should_close,
)


class TestPortfolioExitPolicy(unittest.TestCase):
    def test_clamp_take(self):
        self.assertEqual(clamp_take_pct(20.0, floor_pct=4.0, cap_pct=18.0), 18.0)
        self.assertEqual(clamp_take_pct(2.0, floor_pct=4.0, cap_pct=18.0), 4.0)

    def test_ml_adjusted_take(self):
        eff, note = compute_ml_adjusted_take(10.0, 1.0)
        self.assertGreaterEqual(eff, 10.0)
        self.assertIn("ml_adj", note)

    def test_trailing_take(self):
        with patch(
            "services.portfolio_exit_policy.trailing_take_params_for_regime",
            return_value=(True, 8.0, 3.0),
        ):
            ok, _ = trailing_take_should_close(12.0, 16.0)
            self.assertTrue(ok)
            ok2, _ = trailing_take_should_close(14.0, 16.0)
            self.assertFalse(ok2)

    def test_melt_up_wider_trailing(self):
        with patch(
            "services.portfolio_exit_policy.trailing_take_params_for_regime",
            return_value=(True, 14.0, 7.0),
        ):
            ok, _ = trailing_take_should_close(12.0, 18.0, regime="melt_up")
            self.assertFalse(ok)
            ok2, _ = trailing_take_should_close(10.0, 18.0, regime="melt_up")
            self.assertTrue(ok2)

    def test_ml_take_melt_up_cap(self):
        eff, _ = compute_ml_adjusted_take(10.0, 8.0, regime="melt_up")
        self.assertGreaterEqual(eff, 12.0)

    def test_resolve_exit_regime_prefers_live(self):
        self.assertEqual(
            resolve_exit_regime(entry_regime="neutral", live_regime="melt_up", use_live=True),
            "melt_up",
        )
        self.assertEqual(
            resolve_exit_regime(entry_regime="melt_up", live_regime="breakdown", use_live=True),
            "breakdown",
        )
        self.assertEqual(
            resolve_exit_regime(entry_regime="melt_up", live_regime="insufficient", use_live=True),
            "melt_up",
        )
        self.assertEqual(
            resolve_exit_regime(entry_regime="neutral", live_regime="melt_up", use_live=False),
            "neutral",
        )

    def test_evaluate_uses_live_regime_for_trailing(self):
        engine = MagicMock()
        # peak 18%, pnl 12% → tight trail closes; melt_up (14/7) holds
        with patch(
            "services.portfolio_exit_policy.peak_pnl_pct_since_entry",
            return_value=18.0,
        ), patch(
            "services.portfolio_exit_policy.trailing_take_params",
            return_value=(True, 8.0, 3.0),
        ), patch(
            "services.portfolio_exit_policy.ml_take_params",
            return_value=(True, 1.5, 4.0, 18.0),
        ), patch(
            "config_loader.get_config_value",
            side_effect=lambda k, d="": {
                "PORTFOLIO_TREND_EXIT_USE_LIVE_REGIME": "true",
                "PORTFOLIO_TREND_MELT_UP_TRAILING_MIN_PROFIT_PCT": "14",
                "PORTFOLIO_TREND_MELT_UP_TRAILING_PULLBACK_PCT": "7",
                "PORTFOLIO_TREND_BREAKDOWN_TRAILING_MIN_PROFIT_PCT": "5",
                "PORTFOLIO_TREND_BREAKDOWN_TRAILING_PULLBACK_PCT": "2",
                "PORTFOLIO_TRAILING_TAKE_ENABLED": "true",
                "PORTFOLIO_TRAILING_MIN_PROFIT_PCT": "8",
                "PORTFOLIO_TRAILING_PULLBACK_PCT": "3",
                "PORTFOLIO_ML_TAKE_ENABLED": "true",
                "PORTFOLIO_ML_TAKE_FACTOR": "1.5",
                "PORTFOLIO_ML_TAKE_FLOOR_PCT": "4",
                "PORTFOLIO_ML_TAKE_CAP_PCT": "18",
                "PORTFOLIO_TREND_MELT_UP_TAKE_CAP_PCT": "35",
                "PORTFOLIO_TREND_BREAKDOWN_TAKE_CAP_PCT": "12",
            }.get(k, d),
        ):
            should, _reason, sig = evaluate_portfolio_exit(
                engine=engine,
                ticker="ALAB",
                entry_price=100.0,
                entry_ts="2026-06-01",
                current_price=112.0,
                buy_take_pct=20.0,
                context_json={"portfolio_trend_regime": "neutral"},
                live_regime="melt_up",
            )
            self.assertFalse(should)
            self.assertEqual(sig, "")

            should2, reason2, sig2 = evaluate_portfolio_exit(
                engine=engine,
                ticker="ALAB",
                entry_price=100.0,
                entry_ts="2026-06-01",
                current_price=112.0,
                buy_take_pct=20.0,
                context_json={"portfolio_trend_regime": "neutral"},
                live_regime="neutral",
            )
            self.assertTrue(should2)
            self.assertEqual(sig2, "TRAILING_TAKE")
            self.assertIn("giveback=", reason2)


if __name__ == "__main__":
    unittest.main()

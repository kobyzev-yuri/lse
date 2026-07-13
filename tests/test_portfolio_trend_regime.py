"""Portfolio 20d trend regime (rule-based MVP)."""

import unittest

from services.portfolio_trend_regime import (
    classify_trend_regime_from_closes,
    exit_policy_note_for_regime,
    regime_from_context,
)


def _closes(n: float, step: float = 0.0, *, count: int = 25) -> list[float]:
    out = [n]
    for i in range(1, count):
        out.append(out[-1] + step)
    return out


class TestPortfolioTrendRegime(unittest.TestCase):
    def test_melt_up(self):
        closes = _closes(100.0, step=2.5, count=30)
        r = classify_trend_regime_from_closes(
            closes,
            thresholds={"melt_up_ret_20d_min": 12.0, "melt_up_sma20_days_min": 10.0,
                        "trend_up_ret_20d_min": 5.0, "breakdown_ret_20d_max": -5.0,
                        "near_high_pct": 2.5, "late_chase_ret_20d_min": 25.0},
        )
        self.assertEqual(r["regime"], "melt_up")

    def test_breakdown(self):
        closes = _closes(100.0, step=-0.6, count=25)
        r = classify_trend_regime_from_closes(closes)
        self.assertEqual(r["regime"], "breakdown")

    def test_insufficient(self):
        r = classify_trend_regime_from_closes([100.0] * 10)
        self.assertEqual(r["regime"], "insufficient")

    def test_regime_from_context(self):
        self.assertEqual(regime_from_context({"portfolio_trend_regime": "melt_up"}), "melt_up")
        self.assertEqual(regime_from_context({}), "neutral")

    def test_exit_policy_note(self):
        self.assertIn("melt_up", exit_policy_note_for_regime("melt_up"))


if __name__ == "__main__":
    unittest.main()

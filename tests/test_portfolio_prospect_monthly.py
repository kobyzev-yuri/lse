"""Unit tests for monthly portfolio allocation / path metrics."""

import unittest

from services.portfolio_prospect_monthly import (
    assign_strategic_bucket,
    capture_band_from_path,
    compute_path_metrics,
)


def _ramp(start: float, step: float, n: int) -> list[float]:
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] + step)
    return out


class TestPortfolioProspectMonthly(unittest.TestCase):
    def test_grind_up_metrics(self):
        closes = _ramp(100.0, 0.8, 130)
        m = compute_path_metrics(closes)
        self.assertGreater(m["market_move_6m_pct"], 50)
        self.assertIn(m["path_tag"], ("grind_up", "melt_up_path", "mixed"))
        self.assertIsNotNone(m["pct_days_above_sma50"])
        band = capture_band_from_path(m)
        self.assertIsNotNone(band["capture_proxy_lo_pct"])
        self.assertGreater(band["capture_proxy_hi_pct"], band["capture_proxy_lo_pct"])

    def test_breakdown_bucket(self):
        closes = _ramp(100.0, -0.4, 130)
        m = compute_path_metrics(closes)
        self.assertEqual(m["path_tag"], "breakdown")
        b = assign_strategic_bucket(
            market_move_6m_pct=m["market_move_6m_pct"],
            prospect_tier="allow",
            path=m,
        )
        self.assertEqual(b, "structurally_weak")

    def test_core_vs_near_high(self):
        closes = _ramp(100.0, 1.0, 130)
        m = compute_path_metrics(closes)
        # Synthesize near-high preference case
        m_near = dict(m)
        m_near["near_6m_high"] = True
        m_near["dist_from_6m_high_pct"] = -1.0
        core = assign_strategic_bucket(
            market_move_6m_pct=80.0,
            prospect_tier="prefer",
            path={**m, "near_6m_high": False, "dist_from_6m_high_pct": -12.0},
        )
        watch = assign_strategic_bucket(
            market_move_6m_pct=80.0,
            prospect_tier="prefer",
            path=m_near,
        )
        self.assertEqual(core, "core_prospect")
        self.assertEqual(watch, "watch_long")


if __name__ == "__main__":
    unittest.main()

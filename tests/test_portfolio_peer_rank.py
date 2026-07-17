"""Unit tests for portfolio peer relative rank (shadow)."""

import unittest
from unittest.mock import patch

from services.portfolio_peer_rank import portfolio_peer_relative_rank


class TestPortfolioPeerRank(unittest.TestCase):
    def test_rank_ok(self):
        report = {
            "clusters": [
                {
                    "id": 1,
                    "medoid": "ASML",
                    "tickers": ["ASML", "KLAC", "LRCX"],
                }
            ]
        }

        def fake_snap(ticker, engine=None):
            rets = {"ASML": 10.0, "KLAC": 5.0, "LRCX": 1.0}
            return {"portfolio_trend_ret_20d_pct": rets[ticker]}

        with patch("services.portfolio_peer_rank._load_shape_map", return_value=report), patch(
            "services.portfolio_trend_regime.portfolio_trend_regime_snapshot",
            side_effect=fake_snap,
        ):
            out = portfolio_peer_relative_rank("LRCX")
            self.assertEqual(out["portfolio_peer_status"], "ok")
            self.assertEqual(out["portfolio_peer_rank"], 3)
            self.assertEqual(out["portfolio_peer_n"], 3)
            self.assertEqual(out["portfolio_peer_ret_vs_medoid_pct"], -9.0)

    def test_missing_cache(self):
        with patch("services.portfolio_peer_rank._load_shape_map", return_value=None):
            out = portfolio_peer_relative_rank("MU")
            self.assertEqual(out["portfolio_peer_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()

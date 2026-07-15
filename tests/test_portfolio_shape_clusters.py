"""Unit tests for shape clustering helpers (no DB)."""

import unittest

import pandas as pd

from services.portfolio_shape_clusters import (
    _clusters_from_corr_threshold,
    correlation_matrix,
    normalize_paths,
)


class TestShapeClusters(unittest.TestCase):
    def test_normalize_and_corr_groups(self):
        idx = pd.date_range("2025-01-01", periods=80, freq="B")
        # A and B same shape; C opposite-ish noise trend
        a = pd.Series(range(1, 81), index=idx, dtype=float)
        b = a * 1.5 + 10
        c = pd.Series(reversed(range(1, 81)), index=idx, dtype=float)
        norm = normalize_paths({"AAA": a, "BBB": b, "CCC": c})
        corr = correlation_matrix(norm)
        self.assertGreater(float(corr.loc["AAA", "BBB"]), 0.99)
        groups = _clusters_from_corr_threshold(corr, 0.9)
        # AAA+BBB together, CCC alone (or opposite cluster)
        joined = {frozenset(g) for g in groups}
        self.assertIn(frozenset({"AAA", "BBB"}), joined)


if __name__ == "__main__":
    unittest.main()

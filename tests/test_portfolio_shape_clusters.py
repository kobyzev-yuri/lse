"""Unit tests for shape clustering helpers (no DB)."""

import unittest

import pandas as pd

from services.portfolio_shape_clusters import (
    _clusters_from_corr_threshold,
    _clusters_hierarchical,
    _empty_shape_cluster_report,
    correlation_matrix,
    downsample_closes,
    normalize_paths,
    shape_cluster_method_ru,
    spark_closes_from_series,
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

    def test_hierarchical_distance_cut(self):
        idx = pd.date_range("2025-01-01", periods=80, freq="B")
        a = pd.Series(range(1, 81), index=idx, dtype=float)
        b = a * 1.5 + 10
        c = pd.Series(reversed(range(1, 81)), index=idx, dtype=float)
        corr = correlation_matrix(normalize_paths({"AAA": a, "BBB": b, "CCC": c}))
        groups = _clusters_hierarchical(corr, distance_threshold=0.12, max_clusters=None)
        joined = {frozenset(g) for g in groups}
        self.assertIn(frozenset({"AAA", "BBB"}), joined)

    def test_downsample_and_spark_closes(self):
        vals = list(range(1, 201))
        ds = downsample_closes(vals, max_points=80)
        self.assertEqual(len(ds), 80)
        self.assertEqual(ds[0], 1.0)
        self.assertEqual(ds[-1], 200.0)
        idx = pd.date_range("2025-01-01", periods=120, freq="B")
        s = pd.Series(range(1, 121), index=idx, dtype=float)
        sparks = spark_closes_from_series({"rblx": s}, max_points=40)
        self.assertIn("RBLX", sparks)
        self.assertEqual(len(sparks["RBLX"]), 40)

    def test_method_ru_mentions_pearson_not_cosine(self):
        text = shape_cluster_method_ru()
        self.assertIn("Пирсона", text)
        self.assertIn("не cosine", text.lower())
        self.assertIn("1 − corr", text)

    def test_empty_report_for_cache_only_ssr(self):
        empty = _empty_shape_cluster_report(
            tickers=["AAA", "BBB"],
            lookback_trading_days=126,
            corr_min=0.88,
            method="hierarchical",
            mode="shape",
            max_clusters=0,
            distance_threshold=0.12,
            cache_source="cache_miss",
        )
        self.assertEqual(empty["n_tickers_ok"], 0)
        self.assertEqual(empty["clusters"], [])
        self.assertEqual(empty["cache_source"], "cache_miss")
        self.assertTrue(empty.get("method_ru"))


if __name__ == "__main__":
    unittest.main()

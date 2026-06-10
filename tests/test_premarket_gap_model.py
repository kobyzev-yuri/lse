# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from services.premarket_gap_model import (
    evaluate_pooled_gap_model_from_rows,
    fit_pooled_gap_model,
    predict_from_artifact,
)


class TestPremarketGapModel(unittest.TestCase):
    def _rows(self):
        rows = []
        for i in range(40):
            pm = -2.0 + i * 0.1
            sec = pm * 0.4
            rows.append(
                {
                    "trade_date": f"2026-04-{(i % 20) + 1:02d}",
                    "symbol": "SNDK" if i % 2 else "MU",
                    "premarket_gap_pct": pm,
                    "pred_sector_gap_pct": sec,
                    "open_gap_pct": 0.6 * pm + 0.2 * sec,
                }
            )
        return rows

    def test_fit_and_predict_artifact(self):
        artifact = fit_pooled_gap_model(self._rows(), min_rows=20, l2=1.0)
        self.assertTrue(artifact["ready"])
        pred, meta = predict_from_artifact(
            artifact,
            symbol="MU",
            premarket_gap_pct=1.0,
            pred_sector_gap_pct=0.4,
        )
        self.assertIsNotNone(pred)
        self.assertEqual(meta["model_version"], "pooled_ridge_v2")

    def test_eval_requires_enough_rows(self):
        out = evaluate_pooled_gap_model_from_rows(self._rows()[:10], min_train_rows=20)
        self.assertEqual(out["mode"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()

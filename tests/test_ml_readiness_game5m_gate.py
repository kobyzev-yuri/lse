"""GAME_5M entry CatBoost readiness gate."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestGame5mReadinessGate(unittest.TestCase):
    def test_requires_n_valid_80(self):
        from scripts.run_ml_train_readiness_cron import _gate_game5m

        data = {
            "status": "ok",
            "auc_valid": 0.58,
            "n_valid": 49,
            "n_train": 200,
        }
        with patch.dict(os.environ, {}, clear=False):
            gate = _gate_game5m(data)
        self.assertFalse(gate["ready"])
        self.assertTrue(any("n_valid" in r for r in gate["reasons"]))

    def test_passes_when_auc_and_n_valid_ok(self):
        from scripts.run_ml_train_readiness_cron import _gate_game5m

        data = {
            "status": "ok",
            "auc_valid": 0.58,
            "n_valid": 80,
            "n_train": 200,
        }
        gate = _gate_game5m(data)
        self.assertTrue(gate["ready"])


if __name__ == "__main__":
    unittest.main()

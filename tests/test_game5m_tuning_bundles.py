# -*- coding: utf-8 -*-
"""Tests for GAME_5M multi-parameter tuning bundles."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.game5m_tuning_bundles import OVERNIGHT_MULTIDAY_V1, get_bundle, list_bundles
from services.game5m_tuning_policy import (
    apply_game5m_bundle,
    rollback_game5m_experiment_applied,
    validate_game5m_bundle,
)
from services.multiday_lr_gate import hold_gate_should_defer_exit


class TestHoldGateDeferHelper(unittest.TestCase):
    def test_apply_mode_defer(self):
        self.assertTrue(
            hold_gate_should_defer_exit(
                {"mode": "apply", "status": "ok", "would_defer_exit": True},
            )
        )

    def test_log_only_no_defer(self):
        self.assertFalse(
            hold_gate_should_defer_exit(
                {"mode": "log_only", "status": "ok", "would_defer_exit": True},
            )
        )


class TestTuningBundles(unittest.TestCase):
    def test_list_and_get(self):
        bundles = list_bundles()
        self.assertTrue(any(b["bundle_id"] == "overnight_multiday_v1" for b in bundles))
        b = get_bundle("overnight_multiday_v1")
        self.assertEqual(b.bundle_id, OVERNIGHT_MULTIDAY_V1.bundle_id)
        self.assertIn("GAME_5M_EOD_FLATTEN_ALWAYS", b.changes)
        self.assertEqual(b.changes["GAME_5M_EOD_FLATTEN_ALWAYS"], "false")

    def test_validate_bundle_ok(self):
        ok, reason, vals = validate_game5m_bundle("overnight_multiday_v1", enforce_step_limits=False)
        self.assertTrue(ok, reason)
        self.assertGreater(len(vals), 0)

    def test_ml_freeze_a_and_restore_b_bundle_keys(self):
        a = get_bundle("ml_freeze_a_contours_v1")
        self.assertEqual(a.changes["GAME_5M_CATBOOST_FUSION"], "none")
        b = get_bundle("ml_restore_b_development_v1")
        self.assertEqual(b.changes["GAME_5M_CONTINUATION_ML_ENABLED"], "true")
        self.assertEqual(b.changes["GAME_5M_MULTIDAY_HOLD_GATE_MODE"], "log_only")

    @patch("services.game5m_tuning_policy.update_config_key", return_value=True)
    def test_apply_bundle_dry_run(self, _mock_update):
        ok, payload = apply_game5m_bundle(
            "overnight_multiday_v1",
            source="test",
            dry_run=True,
            enforce_step_limits=False,
        )
        self.assertTrue(ok)
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(len(payload["records"]), len(OVERNIGHT_MULTIDAY_V1.changes))

    @patch("services.game5m_tuning_policy.current_config_value", return_value="legacy")
    @patch("services.game5m_tuning_policy.update_config_key", return_value=True)
    def test_bundle_rollback(self, mock_update, _mock_current):
        ok, payload = apply_game5m_bundle(
            "overnight_multiday_v1",
            source="test",
            dry_run=False,
            enforce_step_limits=False,
        )
        self.assertTrue(ok)
        rb_ok, rb = rollback_game5m_experiment_applied(payload, source="test_rollback")
        self.assertTrue(rb_ok)
        self.assertIsInstance(rb, list)
        self.assertEqual(len(rb), len(payload["records"]))
        self.assertGreaterEqual(mock_update.call_count, len(payload["records"]) * 2)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.game5m_active_tactic import (
    TACTIC_CONTEXT_KEYS,
    enrich_context_with_active_tactic,
    get_active_tactic_snapshot,
    tactic_from_context,
)


class TestGame5mActiveTactic(unittest.TestCase):
    @patch("services.game5m_tuning_ledger.load_ledger")
    def test_get_active_tactic_snapshot_bundle(self, load_ledger):
        load_ledger.return_value = {
            "active_experiment": {
                "experiment_id": "exp-1",
                "bundle_id": "overnight_multiday_v1",
                "kind": "bundle",
                "status": "pending_effect",
            }
        }
        snap = get_active_tactic_snapshot()
        self.assertEqual(snap["active_bundle_id"], "overnight_multiday_v1")
        self.assertEqual(snap["active_experiment_id"], "exp-1")
        self.assertEqual(snap["active_tactic_kind"], "bundle")
        self.assertEqual(snap["active_experiment_status"], "pending_effect")

    @patch("services.game5m_tuning_ledger.load_ledger")
    def test_enrich_at_entry(self, load_ledger):
        load_ledger.return_value = {
            "active_experiment": {
                "experiment_id": "exp-2",
                "bundle_id": "overnight_multiday_v1",
                "status": "observing",
            }
        }
        out = enrich_context_with_active_tactic({"decision": "BUY"}, at_exit=False)
        self.assertEqual(out["active_bundle_id"], "overnight_multiday_v1")
        self.assertEqual(out["decision"], "BUY")

    @patch("services.game5m_tuning_ledger.load_ledger")
    def test_enrich_at_exit_preserves_entry(self, load_ledger):
        load_ledger.return_value = {
            "active_experiment": {
                "experiment_id": "exp-new",
                "bundle_id": "other_bundle",
                "status": "observing",
            }
        }
        entry = {"active_bundle_id": "overnight_multiday_v1", "active_experiment_id": "exp-old"}
        out = enrich_context_with_active_tactic({}, entry_ctx=entry, at_exit=True)
        self.assertEqual(out["active_bundle_id"], "overnight_multiday_v1")
        self.assertEqual(out["active_bundle_id_at_exit"], "other_bundle")

    def test_tactic_from_context(self):
        ctx = {"active_bundle_id": "b1", "noise": 1}
        got = tactic_from_context(ctx)
        self.assertEqual(got["active_bundle_id"], "b1")
        self.assertNotIn("noise", got)
        for k in TACTIC_CONTEXT_KEYS:
            self.assertIn(k, TACTIC_CONTEXT_KEYS)


class TestDealParamsActiveTactic(unittest.TestCase):
    @patch("services.game5m_active_tactic.get_active_tactic_snapshot")
    def test_build_full_entry_context_stamps_tactic(self, snap):
        from services.deal_params_5m import build_full_entry_context

        snap.return_value = {
            "active_bundle_id": "overnight_multiday_v1",
            "active_experiment_id": "e1",
        }
        out = build_full_entry_context({"decision": "BUY", "price": 10.0})
        self.assertEqual(out["active_bundle_id"], "overnight_multiday_v1")
        self.assertEqual(out["active_experiment_id"], "e1")


class TestWeeklyGame5mTacticReview(unittest.TestCase):
    def test_aggregate_trades_by_bundle(self):
        from services.weekly_game5m_tactic_review import _aggregate_trades_by_bundle

        class T:
            def __init__(self, trade_id, ctx):
                self.trade_id = trade_id
                self.context_json = ctx

        class E:
            def __init__(self, trade_id, realized_pct):
                self.trade_id = trade_id
                self.realized_pct = realized_pct

        closed = [
            T(1, json.dumps({"active_bundle_id": "b1"})),
            T(2, json.dumps({"active_bundle_id": "b1"})),
            T(3, {}),
        ]
        effects = [E(1, 1.0), E(2, -0.5), E(3, 0.2)]
        rows = _aggregate_trades_by_bundle(closed, effects)
        by_key = {r["bundle_key"]: r for r in rows}
        self.assertEqual(by_key["b1"]["n"], 2)
        self.assertEqual(by_key["unknown"]["n"], 1)

    def test_write_weekly_review(self):
        from services.weekly_game5m_tactic_review import write_weekly_review

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "review.json"
            path = write_weekly_review({"mode": "weekly_game5m_tactic_review", "closed_trades": 0}, path=p)
            self.assertTrue(Path(path).is_file())
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(data["mode"], "weekly_game5m_tactic_review")

    def test_load_artifact_missing(self):
        from services.trade_effectiveness_analyzer import _load_weekly_game5m_tactic_review_artifact

        with patch("services.weekly_game5m_tactic_review.default_weekly_review_path") as dp:
            dp.return_value = Path("/nonexistent/weekly_review.json")
            out = _load_weekly_game5m_tactic_review_artifact(days=7)
        self.assertEqual(out["status"], "missing_artifact")


if __name__ == "__main__":
    unittest.main()

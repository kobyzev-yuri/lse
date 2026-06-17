# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import patch

from services.decision_stack.earnings_trust_monitor import (
    build_earnings_trust_gate_monitor,
    runtime_gate_summary_for_ticker,
)


class TestEarningsTrustMonitor(unittest.TestCase):
    def test_runtime_gate_summary_inactive(self):
        with patch(
            "services.decision_stack.earnings_trust_monitor.build_earnings_trust_runtime",
            return_value={"active": False, "reason": "no_recent_postmortem"},
        ):
            out = runtime_gate_summary_for_ticker("ZZZ")
        self.assertFalse(out.get("active"))
        self.assertEqual(out.get("reason"), "no_recent_postmortem")

    def test_runtime_gate_summary_apply_downgrade(self):
        sample = {
            "active": True,
            "strength": -0.4,
            "would_downgrade": True,
            "detail_ru": "test",
            "runtime_role": "source",
            "source_symbol": "ORCL",
            "event_date": "2026-06-10",
            "trust_labels": {},
        }
        env = {"DECISION_STACK_EARNINGS_TRUST_GATE_MODE": "apply"}
        with patch(
            "services.decision_stack.earnings_trust_monitor.build_earnings_trust_runtime",
            return_value=sample,
        ), patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            out = runtime_gate_summary_for_ticker("ORCL")
        self.assertTrue(out.get("active"))
        self.assertEqual(out.get("action"), "downgrade")
        self.assertEqual(out.get("shadow_if_core_bull"), "HOLD")

    def test_build_monitor_empty_trades(self):
        with patch(
            "services.decision_stack.earnings_trust_monitor.collect_earnings_trust_watchlist_tickers",
            return_value=[],
        ):
            out = build_earnings_trust_gate_monitor([], limit=10)
        self.assertEqual(out.get("mode"), "earnings_trust_gate_monitor")
        self.assertEqual(out.get("watchlist_count"), 0)


if __name__ == "__main__":
    unittest.main()

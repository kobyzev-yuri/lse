"""Tests for premarket gap LLM context (PRE_MARKET only)."""
from __future__ import annotations

import unittest

from services.premarket_llm_context import (
    attach_premarket_entry_context,
    build_premarket_entry_context_block,
)


class TestPremarketLlmContext(unittest.TestCase):
    def test_skips_outside_premarket(self):
        td = attach_premarket_entry_context(
            "SNDK",
            {"close": 100.0},
            decision_5m={"session_phase": "REGULAR"},
        )
        self.assertNotIn("premarket_entry_context_block", td)

    def test_builds_block_from_decision_5m(self):
        d5 = {
            "session_phase": "PRE_MARKET",
            "premarket_gap_pct": 2.5,
            "premarket_last": 102.5,
            "prev_close": 100.0,
            "minutes_until_open": 45,
            "premarket_gap_baseline": {
                "signal": "bullish_gap",
                "action": "boost",
                "entry_advice": "ALLOW",
                "reason": "premarket gap +2.50% >= bullish baseline +1.00%",
                "should_take_watch": False,
            },
            "ticker_open_gap_predicted_pct": 1.8,
            "ticker_open_gap_predicted_source": "ticker_ols_v2_premarket_blend",
            "ticker_open_gap_confidence": 0.62,
            "ticker_open_gap_model_n_train": 120,
            "macro_predicted_sector_gap_pct": 0.5,
            "macro_sector_proxy": "SMH",
            "entry_advice": "ALLOW",
        }
        block = build_premarket_entry_context_block("SNDK", d5)
        self.assertIn("SNDK", block)
        self.assertIn("+2.50%", block)
        self.assertIn("bullish_gap", block)
        self.assertIn("ticker OLS", block)
        self.assertIn("SMH", block)

        td = attach_premarket_entry_context("SNDK", {"close": 102.5}, decision_5m=d5)
        self.assertEqual(td.get("session_phase"), "PRE_MARKET")
        self.assertIn("premarket_entry_context_block", td)
        self.assertIn("Гэп к вчерашнему закрытию", td.get("premarket_note") or "")

    def test_idempotent_when_block_present(self):
        existing = {"premarket_entry_context_block": "already"}
        td = attach_premarket_entry_context(
            "SNDK",
            existing,
            decision_5m={"session_phase": "PRE_MARKET", "premarket_gap_pct": 1.0},
        )
        self.assertEqual(td.get("premarket_entry_context_block"), "already")


if __name__ == "__main__":
    unittest.main()

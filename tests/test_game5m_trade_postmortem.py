"""Tests for services/game5m_trade_postmortem classification and tactics."""
from __future__ import annotations

import unittest

from services.game5m_trade_postmortem import (
    aggregate_tactics_state,
    build_tactic_recommendations,
    classify_trade,
)


class TestClassifyTrade(unittest.TestCase):
    def test_cien_like_weak_entry_and_hold(self):
        tags, rec = classify_trade(
            entry_ctx={
                "technical_entry_branch": "buy_rth_momentum",
                "catboost_entry_proba_good": 0.5213,
                "momentum_2h_pct": 1.48,
            },
            exit_ctx={"exit_detail": "overnight_eod_flat_loss"},
            pnl_pct=-2.5,
            mfe_pct=0.47,
            mae_pct=-2.63,
            exit_type="TIME_EXIT",
            exit_detail="overnight_eod_flat_loss",
            is_open=False,
            hold_minutes=175.0,
        )
        self.assertIn("A", tags)
        self.assertIn("C", tags)
        self.assertEqual(rec, "train_exit")

    def test_strong_take_profit_ok(self):
        tags, rec = classify_trade(
            entry_ctx={"technical_entry_branch": "strong_buy_rsi", "catboost_entry_proba_good": 0.55},
            exit_ctx={},
            pnl_pct=3.5,
            mfe_pct=4.0,
            mae_pct=-0.2,
            exit_type="TAKE_PROFIT",
            exit_detail="",
            is_open=False,
            hold_minutes=60.0,
        )
        self.assertEqual(tags, [])
        self.assertEqual(rec, "ok")

    def test_gave_back_mfe_is_exit(self):
        tags, rec = classify_trade(
            entry_ctx={"technical_entry_branch": "strong_buy_rsi"},
            exit_ctx={},
            pnl_pct=0.5,
            mfe_pct=3.0,
            mae_pct=-1.0,
            exit_type="TIME_EXIT",
            exit_detail="",
            is_open=False,
            hold_minutes=120.0,
        )
        self.assertIn("B", tags)
        self.assertEqual(rec, "train_exit")


class TestTacticsAggregate(unittest.TestCase):
    def test_entry_focus_from_jul1_like_sessions(self):
        sessions = [
            {
                "session_date_msk": "2026-07-01",
                "trades": [
                    {"tags": ["A", "C"], "training_recommendation": "train_exit", "mfe_pct": 0.2, "pnl_pct": -2.5, "catboost_p_good": 0.52},
                    {"tags": ["A"], "training_recommendation": "train_entry", "mfe_pct": 0.9, "catboost_p_good": 0.52},
                    {"tags": ["A"], "training_recommendation": "train_entry", "mfe_pct": 0.4, "catboost_p_good": 0.45},
                ],
            }
        ]
        state = aggregate_tactics_state(sessions, window_days=14)
        self.assertEqual(state["tag_counts_rolling"]["A"], 3)
        recs = build_tactic_recommendations(state)
        ids = {r["id"] for r in recs}
        self.assertIn("entry_fusion_tighten", ids)


if __name__ == "__main__":
    unittest.main()

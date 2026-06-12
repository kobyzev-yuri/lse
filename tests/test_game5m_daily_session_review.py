"""Tests for scripts/game5m_daily_session_review.py helpers."""
from __future__ import annotations

import unittest
from datetime import date, datetime

from scripts.game5m_daily_session_review import (
    _block_new_buy_cutoff_msk,
    _eod_flat_exit_fields,
    _is_late_buy_near_close,
    _rth_close_msk,
)


class TestNearCloseCutoff(unittest.TestCase):
    def test_rth_close_june_2026_edt(self):
        close = _rth_close_msk(date(2026, 6, 11))
        self.assertEqual(close, datetime(2026, 6, 11, 23, 0))

    def test_block_cutoff_90_min(self):
        cutoff = _block_new_buy_cutoff_msk(date(2026, 6, 11), 90)
        self.assertEqual(cutoff, datetime(2026, 6, 11, 21, 30))

    def test_amd_buy_not_late_with_90_min_block(self):
        # AMD 11.06 opened 20:55 MSK — inside allowed window.
        self.assertFalse(_is_late_buy_near_close("2026-06-11 20:55:00", date(2026, 6, 11), 90))

    def test_buy_at_cutoff_is_late(self):
        self.assertTrue(_is_late_buy_near_close("2026-06-11 21:30:00", date(2026, 6, 11), 90))

    def test_buy_after_cutoff_is_late(self):
        self.assertTrue(_is_late_buy_near_close("2026-06-11 21:45:00", date(2026, 6, 11), 90))


class TestEodFlatFields(unittest.TestCase):
    def test_extracts_position_state(self):
        ctx = {
            "exit_detail": "overnight_eod_flat",
            "position_state_v2": {
                "distance_to_take_pct": 0.0026,
                "momentum_2h_pct": 0.6422,
                "take_pct": 4.0,
            },
            "continuation_gate": {"apply_skip_reason": "trail_pullback_exceeded"},
        }
        out = _eod_flat_exit_fields(ctx)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["distance_to_take_pct"], 0.0026)
        self.assertEqual(out["continuation_apply_skip_reason"], "trail_pullback_exceeded")

    def test_skips_non_eod_exit(self):
        self.assertIsNone(_eod_flat_exit_fields({"exit_detail": "take_profit"}))


if __name__ == "__main__":
    unittest.main()

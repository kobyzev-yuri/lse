"""ML refresh dispatcher: frozen list A vs active list B contours."""
from __future__ import annotations

from scripts.run_ml_refresh_dispatcher import (
    ACTIVE_REFRESH_CONTOURS,
    ML_FROZEN_REFRESH_CONTOURS,
    NIGHTLY_SEQUENCE,
    WEEKLY_FULL_CONTOURS,
    _contours_for_slot,
)


def test_frozen_list_a_contours():
    assert "game5m_entry" in ML_FROZEN_REFRESH_CONTOURS
    assert "game5m_entry_bar_v2" in ML_FROZEN_REFRESH_CONTOURS
    assert "recovery" in ML_FROZEN_REFRESH_CONTOURS
    assert "gap_forecast" in ML_FROZEN_REFRESH_CONTOURS
    assert "open_path" in ML_FROZEN_REFRESH_CONTOURS
    assert "event_reaction_regression" in ML_FROZEN_REFRESH_CONTOURS


def test_active_refresh_keeps_list_b():
    assert "game5m_continuation" in ACTIVE_REFRESH_CONTOURS
    assert "earnings_grid" in ACTIVE_REFRESH_CONTOURS
    assert "portfolio" in ACTIVE_REFRESH_CONTOURS
    assert "multiday_lr" in ACTIVE_REFRESH_CONTOURS


def test_frozen_excluded_from_active():
    for cid in ML_FROZEN_REFRESH_CONTOURS:
        assert cid not in ACTIVE_REFRESH_CONTOURS


def test_nightly_sequence_skips_frozen():
    nightly_ids = {cid for cid, _ in NIGHTLY_SEQUENCE}
    assert not nightly_ids & ML_FROZEN_REFRESH_CONTOURS
    assert "earnings_grid" in nightly_ids
    assert "portfolio" in nightly_ids


def test_weekly_full_skips_frozen():
    assert not WEEKLY_FULL_CONTOURS & ML_FROZEN_REFRESH_CONTOURS
    assert "game5m_continuation" in WEEKLY_FULL_CONTOURS
    assert "multiday_lr" in WEEKLY_FULL_CONTOURS


def test_poll_slot_runs_b_not_a():
    poll = _contours_for_slot("poll", "all")
    poll_ids = {cid for cid, _ in poll}
    assert "game5m_continuation" in poll_ids
    assert "game5m_entry" not in poll_ids
    assert "game5m_entry_bar_v2" not in poll_ids


def test_single_contour_filter_still_runs_frozen():
    """Explicit --contour bypasses freeze (manual ops only)."""
    work = _contours_for_slot("poll", "game5m_entry_bar_v2")
    assert work == [("game5m_entry_bar_v2", [])]

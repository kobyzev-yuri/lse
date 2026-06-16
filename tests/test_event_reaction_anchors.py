# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date

from services.event_reaction_labeling import (
    align_event_date_to_quote_calendar,
    event_reaction_label_threshold_log,
    infer_final_label,
    resolve_event_anchors,
    resolve_peer_outcome_anchor_date,
)


def _weekdays(start: str, n: int) -> list[date]:
    from pandas import bdate_range

    return [d.date() for d in bdate_range(start, periods=n)]


def test_bmo_source_features_anchor_t_minus_one():
    # NVDA reports BMO Tuesday 2026-06-03; features must be Monday close.
    dates = _weekdays("2026-06-01", 5)
    event_d = date(2026, 6, 3)
    anchors = resolve_event_anchors(event_d, "BEFORE_OPEN", dates)
    assert anchors is not None
    assert anchors.features_as_of_date == date(2026, 6, 2)
    assert anchors.outcome_anchor_date == date(2026, 6, 2)
    assert anchors.earnings_market_phase == "BEFORE_OPEN"


def test_amh_peer_anchor_wednesday_reaction():
    # NVDA AMH Tuesday → AMD peer reacts Wednesday open → anchor Tuesday close.
    dates = _weekdays("2026-06-01", 5)
    event_d = date(2026, 6, 3)
    peer_anchor = resolve_peer_outcome_anchor_date(event_d, "AFTER_CLOSE", dates)
    assert peer_anchor == date(2026, 6, 3)
    anchors = resolve_event_anchors(event_d, "AFTER_CLOSE", dates)
    assert anchors is not None
    assert anchors.features_as_of_date == date(2026, 6, 3)
    assert anchors.peer_outcome_anchor_date == date(2026, 6, 3)


def test_bmo_peer_anchor_monday_before_tuesday_reaction():
    dates = _weekdays("2026-06-01", 5)
    event_d = date(2026, 6, 3)
    peer_anchor = resolve_peer_outcome_anchor_date(event_d, "BEFORE_OPEN", dates)
    assert peer_anchor == date(2026, 6, 2)


def test_weekend_calendar_date_bmo_aligns_to_monday():
    # Earnings calendar Saturday → first trade day Monday; features Friday close.
    dates = _weekdays("2026-06-01", 8)
    event_d = date(2026, 6, 6)  # Saturday
    aligned = align_event_date_to_quote_calendar(event_d, dates, phase="BEFORE_OPEN")
    assert aligned == date(2026, 6, 8)  # Monday
    anchors = resolve_event_anchors(event_d, "UNKNOWN", dates)
    assert anchors is not None
    assert anchors.earnings_market_phase == "BEFORE_OPEN"
    assert anchors.features_as_of_date == date(2026, 6, 5)


def test_weekend_amc_aligns_to_friday_close():
    dates = _weekdays("2026-06-01", 5)
    event_d = date(2026, 6, 6)  # Saturday AMC → Friday session
    aligned = align_event_date_to_quote_calendar(event_d, dates, phase="AFTER_CLOSE")
    assert aligned == date(2026, 6, 5)


def test_vol_scaled_threshold_scales_with_volatility():
    low_thr = event_reaction_label_threshold_log(vol_10d_log_ret_std=0.001)
    high_thr = event_reaction_label_threshold_log(vol_10d_log_ret_std=0.05)
    assert high_thr > low_thr
    floor = event_reaction_label_threshold_log()
    move = floor + 0.001
    assert infer_final_label(move, vol_10d_log_ret_std=0.001) == "UP"
    assert infer_final_label(move, vol_10d_log_ret_std=0.05) == "FLAT"

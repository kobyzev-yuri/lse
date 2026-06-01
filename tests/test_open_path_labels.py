"""Tests for open-path rule labels (gap scenario classification)."""
from __future__ import annotations

import math

import pytest

from services.open_path_labels import classify_open_path_scenario


def test_flat_chop_small_gap():
    label, meta = classify_open_path_scenario(
        open_gap_pct=0.3,
        rth_open=100.0,
        rth_close=100.5,
        gap_min_pct=0.8,
    )
    assert label == "open_flat_chop"
    assert meta["fade_from_gap_pct"] == pytest.approx(0.3 - 0.5, abs=0.01)


def test_follow_through_up():
    label, _ = classify_open_path_scenario(
        open_gap_pct=1.5,
        rth_open=100.0,
        rth_close=101.0,
        gap_min_pct=0.8,
        follow_through_log=0.003,
    )
    assert label == "open_follow_through_up"


def test_gap_up_fade():
    label, _ = classify_open_path_scenario(
        open_gap_pct=2.0,
        rth_open=102.0,
        rth_close=101.5,
        gap_min_pct=0.8,
        fade_min_pct=1.5,
    )
    assert label == "open_gap_up_fade"


def test_gap_down_bounce():
    label, _ = classify_open_path_scenario(
        open_gap_pct=-1.2,
        rth_open=98.8,
        rth_close=99.5,
        gap_min_pct=0.8,
        bounce_log=0.005,
    )
    assert label == "open_gap_down_bounce"


def test_gap_down_continuation():
    label, _ = classify_open_path_scenario(
        open_gap_pct=-1.5,
        rth_open=98.5,
        rth_close=98.0,
        gap_min_pct=0.8,
        continuation_log=-0.003,
    )
    assert label == "open_gap_down_continuation"


def test_strong_gap_chase():
    label, _ = classify_open_path_scenario(
        open_gap_pct=5.0,
        rth_open=105.0,
        rth_close=104.0,
        gap_min_pct=0.8,
        strong_gap_pct=4.0,
        fade_min_pct=1.5,
    )
    assert label == "open_strong_gap_chase"


def test_close_open_log_return():
    from services.open_path_labels import close_open_log_return

    lr = close_open_log_return(rth_open=100.0, rth_close=101.0)
    assert lr == pytest.approx(math.log(1.01), rel=1e-6)

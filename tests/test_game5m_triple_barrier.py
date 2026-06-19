"""Tests for path-dependent triple-barrier labels."""
from __future__ import annotations

import pandas as pd

from services.game5m_triple_barrier import (
    ENTRY_BAR_ML_SCHEMA,
    TripleBarrierConfig,
    forward_excursion_pct,
    triple_barrier_forward,
)


def _bars(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_upper_touches_first():
    df = _bars(
        [
            {"datetime": "2026-06-01 10:00:00", "Open": 100, "High": 100.2, "Low": 99.8, "Close": 100},
            {"datetime": "2026-06-01 10:05:00", "Open": 100, "High": 101.5, "Low": 99.9, "Close": 101.2},
        ]
    )
    cfg = TripleBarrierConfig(upper_pct=1.0, lower_pct=1.0, max_bars=5, max_minutes=60, cost_bps=0)
    res = triple_barrier_forward(df, 0, config=cfg)
    assert res.label == "upper"
    assert res.y_entry_good is True
    assert res.bars_forward == 1


def test_lower_touches_first():
    df = _bars(
        [
            {"datetime": "2026-06-01 10:00:00", "Open": 100, "High": 100.2, "Low": 99.8, "Close": 100},
            {"datetime": "2026-06-01 10:05:00", "Open": 100, "High": 100.1, "Low": 98.5, "Close": 99},
        ]
    )
    cfg = TripleBarrierConfig(upper_pct=1.0, lower_pct=1.0, max_bars=5, max_minutes=60, cost_bps=0)
    res = triple_barrier_forward(df, 0, config=cfg)
    assert res.label == "lower"
    assert res.y_entry_good is False


def test_time_barrier_when_no_touch():
    df = _bars(
        [
            {"datetime": "2026-06-01 10:00:00", "Open": 100, "High": 100.2, "Low": 99.8, "Close": 100},
            {"datetime": "2026-06-01 10:05:00", "Open": 100, "High": 100.4, "Low": 99.7, "Close": 100.1},
            {"datetime": "2026-06-01 10:10:00", "Open": 100.1, "High": 100.5, "Low": 99.9, "Close": 100.2},
        ]
    )
    cfg = TripleBarrierConfig(upper_pct=2.0, lower_pct=2.0, max_bars=2, max_minutes=120, cost_bps=0)
    res = triple_barrier_forward(df, 0, config=cfg)
    assert res.label == "time"
    assert res.bars_forward == 2


def test_cost_bps_raises_upper_hurdle():
    df = _bars(
        [
            {"datetime": "2026-06-01 10:00:00", "Open": 100, "High": 100.2, "Low": 99.8, "Close": 100},
            {"datetime": "2026-06-01 10:05:00", "Open": 100, "High": 101.05, "Low": 99.9, "Close": 101},
        ]
    )
    no_cost = TripleBarrierConfig(upper_pct=1.0, lower_pct=1.0, max_bars=3, max_minutes=60, cost_bps=0)
    with_cost = TripleBarrierConfig(upper_pct=1.0, lower_pct=1.0, max_bars=3, max_minutes=60, cost_bps=20)
    assert triple_barrier_forward(df, 0, config=no_cost).label == "upper"
    assert triple_barrier_forward(df, 0, config=with_cost).label == "time"


def test_forward_excursion_pct():
    df = _bars(
        [
            {"datetime": "2026-06-01 10:00:00", "Open": 100, "High": 100.2, "Low": 99.8, "Close": 100},
            {"datetime": "2026-06-01 10:05:00", "Open": 100, "High": 101, "Low": 99, "Close": 100.5},
        ]
    )
    mfe, mae = forward_excursion_pct(df, 0, max_bars=2)
    assert mfe is not None and mfe > 0.9
    assert mae is not None and mae < 0


def test_entry_bar_schema_version():
    assert ENTRY_BAR_ML_SCHEMA["version"] == "1"
    assert "tb_label" in ENTRY_BAR_ML_SCHEMA["label_columns"]

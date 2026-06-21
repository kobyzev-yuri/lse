"""Tests for chart entry window tensors (leak / split / normalization)."""
from __future__ import annotations

import pandas as pd

from services.game5m_chart_entry_dataset import (
    CHART_ENTRY_ML_SCHEMA,
    assign_time_splits,
    find_decision_bar_index,
    make_sample_id,
    window_includes_future_bars,
    window_tensor_from_df,
)


def _bars(rows: list[dict]) -> pd.DataFrame:
    out = pd.DataFrame(rows)
    d = pd.to_datetime(out["datetime"])
    if d.dt.tz is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    out["datetime"] = d
    return out


def test_make_sample_id_stable():
    a = make_sample_id("AAPL", "2026-06-01T10:00:00-04:00")
    b = make_sample_id("AAPL", "2026-06-01T10:00:00-04:00")
    assert a == b
    assert len(a) == 16


def test_window_ends_at_decision_close():
    df = _bars(
        [
            {"datetime": "2026-06-01 09:30:00", "Open": 100, "High": 100.5, "Low": 99.5, "Close": 100, "Volume": 1000},
            {"datetime": "2026-06-01 09:35:00", "Open": 100, "High": 101, "Low": 99.8, "Close": 100.5, "Volume": 1100},
            {"datetime": "2026-06-01 09:40:00", "Open": 100.5, "High": 102, "Low": 100, "Close": 101, "Volume": 1200},
        ]
    )
    idx = find_decision_bar_index(df, "2026-06-01T09:40:00-04:00")
    assert idx == 2
    win = window_tensor_from_df(df, idx, window_bars=3)
    assert win is not None
    assert win.shape == (3, 5)
    assert abs(float(win[-1, 3])) < 1e-4  # anchor close pct = 0


def test_window_no_future_leak():
    df = _bars(
        [
            {"datetime": "2026-06-01 09:30:00", "Open": 100, "High": 100.5, "Low": 99.5, "Close": 100, "Volume": 1000},
            {"datetime": "2026-06-01 09:35:00", "Open": 100, "High": 101, "Low": 99.8, "Close": 100.5, "Volume": 1100},
            {"datetime": "2026-06-01 09:40:00", "Open": 100.5, "High": 102, "Low": 100, "Close": 101, "Volume": 1200},
            {"datetime": "2026-06-01 09:45:00", "Open": 200, "High": 250, "Low": 200, "Close": 240, "Volume": 99999},
        ]
    )
    idx = 1
    win = window_tensor_from_df(df, idx, window_bars=2)
    assert win is not None
    assert not window_includes_future_bars(df, idx, win, window_bars=2)
    assert float(win[:, :4].max()) < 50.0


def test_insufficient_history_returns_none():
    df = _bars(
        [{"datetime": "2026-06-01 09:30:00", "Open": 100, "High": 100.5, "Low": 99.5, "Close": 100, "Volume": 1}]
    )
    assert window_tensor_from_df(df, 0, window_bars=48) is None


def test_assign_time_splits_last_fraction_valid():
    ts = [f"2026-06-{i:02d}T10:00:00-04:00" for i in range(1, 11)]
    splits = assign_time_splits(ts, valid_ratio=0.2)
    assert len(splits) == 10
    assert splits.count("valid") == 2
    assert splits[8] == "valid"
    assert splits[9] == "valid"
    assert splits[0] == "train"


def test_schema_version():
    assert CHART_ENTRY_ML_SCHEMA["version"] == "1"
    assert "open_pct_anchor" in CHART_ENTRY_ML_SCHEMA["features"]

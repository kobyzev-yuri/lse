"""Tests for entry E3 / hold quality shadow feature builders."""
from __future__ import annotations

import pandas as pd

from services.game5m_entry_e3_signal import build_entry_e3_feature_row
from services.game5m_hold_quality_signal import row_vector_from_live_hold


def test_build_entry_e3_feature_row_shape():
    colnames, row = build_entry_e3_feature_row(
        "MU",
        {
            "price": 100.0,
            "high_5d": 110.0,
            "low_5d": 90.0,
            "rsi_5m": 40.0,
            "momentum_2h_pct": 1.0,
            "momentum_rth_today_pct": 0.5,
            "volatility_5m_pct": 0.6,
            "pullback_from_high_pct": 1.0,
            "bars_count": 100,
            "momentum_rth_today_bars": 5,
            "prob_up": 0.55,
            "prob_down": 0.45,
            "decision_5m_bar_open_et": "2026-06-02T11:00:00-04:00",
        },
    )
    assert colnames[0] == "ticker"
    assert row[0] == "MU"
    assert len(row) == len(colnames)
    assert row[colnames.index("rsi_5m")] == 40.0


def test_row_vector_from_live_hold():
    et = pd.Timestamp("2026-06-02 10:00:00", tz="America/New_York")
    bt = pd.Timestamp("2026-06-02 11:00:00", tz="America/New_York")
    row = row_vector_from_live_hold(
        ticker="MU",
        entry_price=100.0,
        entry_ts_et=et,
        bar_ts_et=bt,
        ref_close=101.0,
        entry_ctx={"rsi_5m": 45.0, "prob_up": 0.6},
        exit_features={"rsi_5m": 50.0, "momentum_2h_pct": 0.5},
    )
    assert row is not None
    assert row[0] == "MU"
    assert len(row) > 10

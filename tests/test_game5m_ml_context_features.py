"""Tests for GAME_5M ML context features (entry/hold enrich, no leak helpers)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from services.game5m_chart_entry_dataset import window_tensor_from_df, window_tensor_with_context
from services.game5m_ml_context_features import (
    ENTRY_CONTEXT_NUMERIC_KEYS,
    build_entry_context_features,
    entry_snapshot_from_context,
    hold_state_features,
    infer_kb_news_impact_label,
    kb_news_impact_enc,
    session_phase_enc_from_ts,
)


def test_kb_news_impact_enc():
    assert kb_news_impact_enc("нейтрально") == 0.0
    assert kb_news_impact_enc("позитив") > 0
    assert kb_news_impact_enc("негатив (вход отложен)") < 0


def test_infer_kb_news_from_sentiment():
    news = [{"sentiment_score": 0.2}, {"sentiment_score": 0.3}]
    assert "негатив" in infer_kb_news_impact_label(news)


def test_session_phase_regular_rth():
    ts = pd.Timestamp("2026-06-02 11:00:00", tz="America/New_York")
    enc = session_phase_enc_from_ts(ts)
    assert enc == 2.0  # REGULAR


def test_entry_snapshot_from_context():
    snap = entry_snapshot_from_context({"rsi_5m": 45.0, "kb_news_impact": "позитив", "prob_up": 0.6})
    assert snap["entry_rsi_5m"] == 45.0
    assert snap["entry_prob_up"] == 0.6
    assert snap["entry_kb_news_impact_enc"] > 0


def test_hold_state_features():
    entry = pd.Timestamp("2026-06-02 10:00:00", tz="America/New_York")
    bar = pd.Timestamp("2026-06-02 10:30:00", tz="America/New_York")
    st = hold_state_features(entry_price=100.0, entry_ts_et=entry, bar_ts_et=bar, ref_close=101.0)
    assert abs(st["pnl_pct"] - 1.0) < 1e-6
    assert st["hold_minutes"] == 30.0


def test_build_entry_context_features_defaults():
    ctx = build_entry_context_features(
        ticker="AAPL",
        bar_ts_et="2026-06-02T11:00:00-04:00",
        features={"rsi_5m": 50.0},
        engine=None,
        kb_news=[],
    )
    assert "session_phase_enc" in ctx
    assert ctx["kb_news_count"] == 0.0


def test_window_tensor_with_context_broadcast():
    df = pd.DataFrame(
        [
            {
                "datetime": "2026-06-01 09:30:00",
                "Open": 100,
                "High": 100.5,
                "Low": 99.5,
                "Close": 100,
                "Volume": 1000,
            },
            {
                "datetime": "2026-06-01 09:35:00",
                "Open": 100,
                "High": 101,
                "Low": 99.8,
                "Close": 100.5,
                "Volume": 1100,
            },
        ]
    )
    d = pd.to_datetime(df["datetime"]).dt.tz_localize("America/New_York", ambiguous=True)
    df["datetime"] = d
    win = window_tensor_from_df(df, 1, window_bars=2)
    assert win is not None
    row = {k: 0.0 for k in ENTRY_CONTEXT_NUMERIC_KEYS}
    row["kb_news_impact_enc"] = 1.0
    win2 = window_tensor_with_context(win, row, include_context=True)
    assert win2.shape[0] == 2
    assert win2.shape[1] == 5 + len(ENTRY_CONTEXT_NUMERIC_KEYS)
    assert np.allclose(win2[0, 5:], win2[1, 5:])

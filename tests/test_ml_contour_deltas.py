"""Tests for ML contour delta counters."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.ml_contour_deltas import (
    count_deltas_for_contour,
    count_strategy_buys_since,
    count_strategy_closed_since,
)
from services.ml_contour_refresh import get_contour_spec


def _mock_engine_scalar_sequence(values: list):
    engine = MagicMock()
    conn = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    engine.connect.return_value = ctx
    conn.execute.return_value.scalar.side_effect = values
    return engine


def test_count_strategy_closed_since_with_since():
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    engine = _mock_engine_scalar_sequence([7])
    assert count_strategy_closed_since(engine, "GAME_5M", since) == 7
    conn = engine.connect.return_value.__enter__.return_value
    call_args = conn.execute.call_args
    sql = str(call_args[0][0])
    params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
    assert "SELL" in sql
    assert params["since"] == since


def test_count_strategy_closed_since_all_time():
    engine = _mock_engine_scalar_sequence([42])
    assert count_strategy_closed_since(engine, "GAME_5M", None) == 42


def test_game5m_entry_deltas_use_closed_not_buy(monkeypatch):
    spec = get_contour_spec("game5m_entry")
    log = {"last_apply_at_utc": "2026-06-01T00:00:00+00:00"}
    calls: list[str] = []

    def _track_closed(engine, strategy, since):
        calls.append("closed")
        return 3

    def _track_buy(engine, strategy, since):
        calls.append("buy")
        return 99

    monkeypatch.setattr("services.ml_contour_deltas.count_strategy_closed_since", _track_closed)
    monkeypatch.setattr("services.ml_contour_deltas.count_strategy_buys_since", _track_buy)

    out = count_deltas_for_contour(MagicMock(), spec, log)
    assert out == {"new_units_apply": 3, "new_units_train": 3}
    assert calls == ["closed", "closed"]
    assert "buy" not in calls


def test_count_strategy_buys_since_still_available():
    engine = _mock_engine_scalar_sequence([5])
    assert count_strategy_buys_since(engine, "PORTFOLIO", None) == 5


def test_multiday_lr_deltas_use_quotes(monkeypatch):
    spec = get_contour_spec("multiday_lr")
    calls: list[str] = []

    def _quotes(engine, since, *, tickers=None):
        calls.append("quotes")
        return 11

    monkeypatch.setattr("services.ml_contour_deltas.count_quotes_daily_rows_since", _quotes)
    monkeypatch.setattr("services.ml_contour_deltas._multiday_lr_ticker_universe", lambda _e: ["NVDA"])
    out = count_deltas_for_contour(MagicMock(), spec, {})
    assert out["new_units_apply"] == 11
    assert calls == ["quotes", "quotes"]


def test_recovery_deltas_use_time_exit(monkeypatch):
    spec = get_contour_spec("recovery")

    def _recovery(engine, since):
        return 4

    monkeypatch.setattr("services.ml_contour_deltas.count_recovery_export_rows_since", _recovery)
    out = count_deltas_for_contour(MagicMock(), spec, {})
    assert out == {"new_units_apply": 4, "new_units_train": 4}


def test_gap_forecast_deltas(monkeypatch):
    spec = get_contour_spec("gap_forecast")

    def _gap(engine, since):
        return 9

    monkeypatch.setattr("services.ml_contour_deltas.count_gap_forecast_complete_since", _gap)
    out = count_deltas_for_contour(MagicMock(), spec, {})
    assert out == {"new_units_apply": 9, "new_units_train": 9}

"""Tests for earnings calendar ticker universe."""
from __future__ import annotations

from unittest.mock import patch

from services.earnings_intelligence_universe import (
    DEFAULT_EARNINGS_TRACK_TICKERS,
    get_earnings_calendar_tickers,
)


def test_default_earnings_track_includes_nvda_and_tsm():
    assert "NVDA" in DEFAULT_EARNINGS_TRACK_TICKERS
    assert "TSM" in DEFAULT_EARNINGS_TRACK_TICKERS
    assert "MU" in DEFAULT_EARNINGS_TRACK_TICKERS


def test_calendar_tickers_union_intelligence_even_with_slim_config(monkeypatch):
    monkeypatch.setenv("EARNINGS_TRACK_TICKERS", "MU,LITE")
    monkeypatch.setenv("YFINANCE_EARNINGS_TICKERS", "")
    tickers = get_earnings_calendar_tickers()
    assert "MU" in tickers
    assert "LITE" in tickers
    assert "NVDA" in tickers


def test_yfinance_explicit_list_still_unions_intelligence(monkeypatch):
    monkeypatch.setenv("YFINANCE_EARNINGS_TICKERS", "ORCL")
    monkeypatch.setenv("EARNINGS_TRACK_TICKERS", "")
    tickers = get_earnings_calendar_tickers()
    assert "ORCL" in tickers
    assert "NVDA" in tickers


def test_yfinance_fetcher_uses_calendar_helper():
    with patch(
        "services.earnings_intelligence_universe.get_earnings_calendar_tickers",
        return_value=["NVDA", "MU"],
    ):
        from services.yfinance_earnings_fetcher import _yfinance_earnings_ticker_list

        assert _yfinance_earnings_ticker_list() == ["NVDA", "MU"]

"""Unit tests for Google Sheet SA news parsers (no live Google API)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from services.sa_sheet_feed import (
    expand_items_for_kb,
    parse_rows,
    parse_symbols,
    parse_timestamp,
    row_complete,
    symbols_for_row,
)


NY = ZoneInfo("America/New_York")


def test_parse_timestamp_et_suffixes():
    for s in (
        "Aug 04, 2026, 6:45 AM ET",
        "Aug 04, 2026, 6:45 AM EDT",
        "Aug 04, 2026, 6:45 AM EST",
    ):
        dt = parse_timestamp(s, NY)
        assert dt.year == 2026 and dt.month == 8 and dt.day == 4
        assert dt.hour == 6 and dt.minute == 45
        assert dt.tzinfo is not None


def test_parse_timestamp_bad():
    with pytest.raises(ValueError):
        parse_timestamp("not-a-date", NY)


def test_parse_symbols_and_macro():
    assert parse_symbols("AAPL, MSFT") == ["AAPL", "MSFT"]
    assert parse_symbols(" aapl , ") == ["AAPL"]
    assert symbols_for_row("-") == ["MACRO"]
    assert symbols_for_row("") == ["MACRO"]
    assert symbols_for_row("SNDK") == ["SNDK"]


def test_row_complete():
    assert row_complete(["t", "u", "title", "text", "-"])
    assert not row_complete(["t", "u", "title", "text"])  # missing symbols
    assert not row_complete(["t", "", "title", "text", "-"])


def test_parse_rows_stops_at_incomplete_and_skips_header():
    rows = [
        ["Time", "URL", "Title", "Text", "Symbols"],
        [
            "Aug 04, 2026, 6:45 AM ET",
            "https://seekingalpha.com/news/4624428-demo",
            "Demo title",
            "Body text",
            "AAPL, MSFT",
        ],
        [
            "Aug 04, 2026, 7:00 AM ET",
            "https://seekingalpha.com/news/1",
            "Incomplete",
            "",  # mid-write
            "-",
        ],
        [
            "Aug 04, 2026, 8:00 AM ET",
            "https://seekingalpha.com/news/2",
            "Should not parse",
            "x",
            "-",
        ],
    ]
    items = parse_rows(rows, loc=NY)
    assert len(items) == 1
    assert items[0]["title"] == "Demo title"
    assert items[0]["tickers"] == ["AAPL", "MSFT"]
    assert isinstance(items[0]["published_at"], datetime)


def test_expand_items_for_kb_per_ticker():
    items = parse_rows(
        [
            [
                "Aug 04, 2026, 6:45 AM ET",
                "https://seekingalpha.com/news/4624428-am-need-to-know",
                "AM Need to Know",
                "Futures higher",
                "AAPL, MSFT",
            ]
        ],
        loc=NY,
    )
    expanded = expand_items_for_kb(items)
    assert [e["ticker"] for e in expanded] == ["AAPL", "MSFT"]
    assert all(e["id"] == "4624428" for e in expanded)
    assert all(e["provider"] == "sa_google_sheet" for e in expanded)

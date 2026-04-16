"""Tests for KB /news report helpers."""

import pandas as pd

from services.kb_news_report import (
    filter_relevant_kb_rows_for_ticker,
    kb_row_relevant_to_ticker,
    order_kb_display_rows_for_ticker,
)


def test_kb_row_relevant_sandisk_alias_for_sndk():
    row = {"ticker": "MACRO", "content": "Zacks: SanDisk flash", "insight": ""}
    assert kb_row_relevant_to_ticker(pd.Series(row), "SNDK") is True


def test_filter_relevant_keeps_only_ticker_and_matching_macro():
    df = pd.DataFrame(
        [
            {"ticker": "MU", "content": "Micron", "insight": ""},
            {"ticker": "SNDK", "content": "WDC", "insight": ""},
            {"ticker": "MACRO", "content": "random tech", "insight": ""},
        ]
    )
    out = filter_relevant_kb_rows_for_ticker(df, "SNDK")
    assert set(out["ticker"].tolist()) == {"SNDK"}


def test_order_prefers_exact_ticker_over_macro():
    df = pd.DataFrame(
        [
            {
                "ticker": "MACRO",
                "content": "Fresh AMD headline",
                "insight": "",
                "sentiment_score": None,
                "ts": pd.Timestamp("2026-04-16 10:00:00"),
            },
            {
                "ticker": "SNDK",
                "content": "SanDisk NAND note",
                "insight": "",
                "sentiment_score": 0.55,
                "ts": pd.Timestamp("2026-04-15 10:00:00"),
            },
        ]
    )
    out = order_kb_display_rows_for_ticker(df, "SNDK")
    assert out.iloc[0]["ticker"] == "SNDK"


def test_order_macro_mentioning_ticker_before_generic_macro():
    df = pd.DataFrame(
        [
            {
                "ticker": "MACRO",
                "content": "Windows BSOD article",
                "insight": "",
                "sentiment_score": float("nan"),
                "ts": pd.Timestamp("2026-04-16 12:00:00"),
            },
            {
                "ticker": "MACRO",
                "content": "Zacks highlights SanDisk and MU",
                "insight": "",
                "sentiment_score": None,
                "ts": pd.Timestamp("2026-04-16 11:00:00"),
            },
        ]
    )
    out = order_kb_display_rows_for_ticker(df, "SNDK")
    assert "SanDisk" in str(out.iloc[0]["content"])

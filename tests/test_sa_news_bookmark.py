"""Unit tests for SA news bookmark (URL → /v1/news/data → item)."""

from __future__ import annotations

from services.seeking_alpha_finance import (
    flatten_news_data_item,
    parse_sa_news_id,
    resolve_bookmark_ticker,
)


def test_parse_sa_news_id_from_url_and_bare():
    assert (
        parse_sa_news_id(
            "https://seekingalpha.com/news/4624428-am-need-to-know-trump-targets-oil-giants-palantir-beats-estimates-more"
        )
        == "4624428"
    )
    assert parse_sa_news_id("4624428") == "4624428"
    assert parse_sa_news_id("news_id=4624428") == "4624428"
    assert parse_sa_news_id("https://seekingalpha.com/article/123") is None
    assert parse_sa_news_id("") is None


def test_flatten_news_data_item_teaser_and_insights():
    payload = {
        "data": {
            "id": "4624428",
            "type": "news",
            "attributes": {
                "title": "AM Need to Know: oil & PLTR",
                "publishOn": "2026-08-04T06:45:29-04:00",
                "content": "<p>Futures higher. Big Oil scolded: President Trump</p>",
                "isMpwLocked": True,
                "metered": True,
                "quickInsights": [
                    {"question": "PLTR?", "answer": "Beat estimates.", "order": 1},
                ],
            },
            "relationships": {
                "primaryTickers": {"data": [{"id": "603998", "type": "tag"}]},
                "secondaryTickers": {
                    "data": [
                        {"id": "554416", "type": "tag"},
                        {"id": "1198", "type": "tag"},
                    ]
                },
            },
        },
        "included": [
            {"id": "603998", "type": "tag", "attributes": {"name": "SPX", "slug": "spx", "tagType": "index"}},
            {"id": "554416", "type": "tag", "attributes": {"name": "PLTR", "slug": "pltr", "tagType": "stocks"}},
            {"id": "1198", "type": "tag", "attributes": {"name": "XOM", "slug": "xom", "tagType": "stocks"}},
        ],
    }
    item = flatten_news_data_item(payload, prefer_universe=["MSFT", "PLTR", "SNDK"])
    assert item["id"] == "4624428"
    assert item["ticker"] == "PLTR"  # universe hit over SPX primary
    assert "AM Need to Know" in item["title"]
    assert "Futures higher" in item["summary_text"]
    assert "Beat estimates" in item["summary_text"]
    assert item["link"].endswith("/news/4624428")
    assert item["isPaywalled"] is True
    assert "PLTR" in item["tickers"] and "XOM" in item["tickers"]


def test_resolve_bookmark_ticker_spx_to_macro():
    payload = {
        "data": {
            "id": "1",
            "type": "news",
            "attributes": {"title": "x"},
            "relationships": {
                "primaryTickers": {"data": [{"id": "1", "type": "tag"}]},
                "secondaryTickers": {"data": []},
            },
        },
        "included": [
            {"id": "1", "type": "tag", "attributes": {"slug": "spx", "name": "SPX", "tagType": "index"}},
        ],
    }
    assert resolve_bookmark_ticker(payload) == "MACRO"
    assert resolve_bookmark_ticker(payload, ticker_override="MU") == "MU"

"""Tests for notebook news universe + digest helpers (no live API/LLM)."""

from __future__ import annotations

from services.notebook_news_digest import (
    _parse_llm_json,
    build_news_universe,
    dedupe_news_items,
)
from services.seeking_alpha_finance import flatten_news_items


def test_build_news_universe_has_three_groups():
    uni = build_news_universe(equity_only=True)
    assert "group1_portfolio" in uni
    assert "group2_game_5m" in uni
    assert "group3_union" in uni
    assert uni.get("source") in ("notebook", "config", "config_fallback")
    u = set(uni["group3_union"])
    # Notebook samples include MSFT
    if uni.get("source") == "notebook":
        assert "MSFT" in u
    for t in uni["group3_union"]:
        tags = uni["membership"][t]
        assert tags
        assert set(tags) <= {"g1", "g2", "g3", "new", "notebook"}


def test_format_digest_telegram_empty():
    from services.notebook_news_digest import format_digest_telegram

    text = format_digest_telegram(
        {
            "date": "test",
            "filtered": 2,
            "kept": 1,
            "trashed": 1,
            "signals": [{"sym": "MSFT", "text": "Hello", "tac": "<b>Тактика:</b> Hold"}],
            "risks": [],
            "macro": [],
            "newtickers": [],
            "trashNote": "x",
        }
    )
    assert "MSFT" in text
    assert "Hold" in text
    assert "Дайджесты" in text or "/notebook" in text


def test_flatten_news_items():
    payload = {
        "data": [
            {
                "id": "99",
                "type": "news",
                "attributes": {
                    "publishOn": "2026-07-26T10:00:00-04:00",
                    "title": "Hello MSFT",
                    "content": "<p>Body <b>x</b></p>",
                    "isPaywalled": False,
                },
            }
        ]
    }
    rows = flatten_news_items(payload, ticker="msft", limit=5)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "MSFT"
    assert rows[0]["title"] == "Hello MSFT"
    assert "Body" in rows[0]["summary_text"]
    assert "99" in rows[0]["link"]


def test_items_to_kb_articles():
    from services.seeking_alpha_finance import items_to_kb_articles

    items = [
        {
            "id": "4618440",
            "ticker": "MSFT",
            "publishOn": "2026-07-26T10:31:29-04:00",
            "title": "UBS on AI",
            "summary_text": "Body text",
            "link": "https://seekingalpha.com/news/4618440",
        }
    ]
    arts = items_to_kb_articles(items)
    assert len(arts) == 1
    assert arts[0].symbol == "MSFT"
    assert arts[0].source == "Seeking Alpha Finance"
    assert len(arts[0].external_id_raw) >= 24


def test_parse_llm_json_fenced():
    text = '```json\n{"filtered": 3, "kept": 1, "trashed": 2, "signals": [], "risks": [], "macro": [], "newtickers": [], "trashNote": "x"}\n```'
    obj = _parse_llm_json(text)
    assert obj["filtered"] == 3
    assert obj["kept"] == 1


def test_dedupe_news_items_by_link_and_title():
    items = [
        {
            "id": "1",
            "ticker": "INTC",
            "title": "Intel's CapEx Increase Positive for Foundry",
            "summary_text": "short",
            "link": "https://seekingalpha.com/news/4618091?utm=x",
            "src": "Yahoo Finance",
        },
        {
            "id": "2",
            "ticker": "INTC",
            "title": "Intel's CapEx Increase Positive for Foundry",
            "summary_text": "longer body from SA about fabs and Wedbush",
            "link": "https://www.seekingalpha.com/news/4618091",
            "src": "Seeking Alpha Finance",
        },
        {
            "id": "3",
            "ticker": "MSFT",
            "title": "Different story",
            "summary_text": "ok",
            "link": "https://example.com/a",
            "src": "Motley Fool",
        },
        {
            "id": "4",
            "ticker": "INTC",
            "title": "Intel's capex increase positive for foundry!!!",
            "summary_text": "yahoo rewrite",
            "link": "https://finance.yahoo.com/news/other-url",
            "src": "Yahoo Finance",
        },
    ]
    out = dedupe_news_items(items)
    assert len(out) == 2
    by_sym = {x["ticker"]: x for x in out}
    assert by_sym["INTC"]["src"] == "Seeking Alpha Finance"
    assert by_sym["MSFT"]["id"] == "3"

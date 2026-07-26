"""Tests for notebook news universe + digest helpers (no live API/LLM)."""

from __future__ import annotations

from services.notebook_news_digest import _parse_llm_json, build_news_universe
from services.seeking_alpha_finance import flatten_news_items


def test_build_news_universe_has_three_groups():
    uni = build_news_universe(equity_only=True)
    assert "group1_portfolio" in uni
    assert "group2_game_5m" in uni
    assert "group3_union" in uni
    u = set(uni["group3_union"])
    assert u >= set(uni["group1_portfolio"])
    assert u >= set(uni["group2_game_5m"])
    # overlaps tagged
    for t in uni["group3_union"]:
        tags = uni["membership"][t]
        assert tags
        assert set(tags) <= {"portfolio", "game_5m"}


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

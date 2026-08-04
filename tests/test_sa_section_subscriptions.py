"""SA tipsters subscriptions / KB helpers (no live API)."""

from __future__ import annotations

import json
from pathlib import Path

from services.sa_section_subscriptions import (
    SECTION_CATALOG,
    catalog_with_subscriptions,
    enabled_tickers,
    load_subscriptions,
    prepare_section_items_for_kb,
    save_subscriptions,
    section_kb_symbol,
    set_subscription,
    set_subscriptions_bulk,
    subscribe_all_available,
)
from services.seeking_alpha_finance import flatten_section_items


def test_flatten_articles_and_day_watch():
    articles = {
        "data": [
            {
                "id": "4929766",
                "type": "article",
                "attributes": {
                    "title": "Project Prometheus",
                    "publishOn": "2026-08-04T04:35:00-04:00",
                    "themes": ["AI", "Energy"],
                },
                "links": {"self": "/article/4929766-project-prometheus"},
                "relationships": {"primaryTickers": {"data": []}},
            }
        ]
    }
    rows = flatten_section_items(articles, section_id="articles.market-outlook", limit=40)
    assert len(rows) == 1
    kb_rows = prepare_section_items_for_kb(rows, section_id="articles.market-outlook")
    assert kb_rows[0]["ticker"] == "SA:articles.market-outlook"
    assert section_kb_symbol("articles.market-outlook") == "SA:articles.market-outlook"


def test_subscribe_schema_v2_and_ticker_mute(tmp_path: Path, monkeypatch):
    import services.sa_section_subscriptions as m

    subs = tmp_path / "subs.json"
    monkeypatch.setattr(m, "DEFAULT_SUBS_PATH", subs)
    monkeypatch.setattr(m, "get_config_value", lambda k, d=None: d)
    monkeypatch.setattr(
        m,
        "sa_ticker_candidates",
        lambda: {
            "universe": ["MSFT", "AMD"],
            "extras": ["SPY"],
            "all": ["MSFT", "AMD", "SPY"],
            "membership": {"MSFT": ["g1"], "AMD": ["g2"]},
        },
    )

    set_subscription("articles.market-outlook", True, path=subs)
    doc = load_subscriptions(subs)
    assert doc["sections"].get("articles.market-outlook") is True

    set_subscriptions_bulk(
        sections={"articles.latest-articles": True, "articles.market-outlook": False},
        tickers={"SPY": False, "MSFT": True},
        per_group_limit=25,
        path=subs,
    )
    doc2 = load_subscriptions(subs)
    assert doc2["per_group_limit"] == 25
    assert doc2["sections"].get("articles.latest-articles") is True
    assert "articles.market-outlook" not in doc2["sections"]
    assert doc2["tickers"].get("SPY") is False

    enabled = enabled_tickers(["MSFT", "AMD", "SPY"], doc=doc2)
    assert "MSFT" in enabled
    assert "AMD" in enabled  # default_on
    assert "SPY" not in enabled

    subscribe_all_available(path=subs)
    doc3 = load_subscriptions(subs)
    avail = {c["id"] for c in SECTION_CATALOG if c.get("available")}
    assert avail.issubset(set(doc3["sections"]))

    pack = catalog_with_subscriptions(subs_path=subs)
    assert pack["per_group_limit"] == 25
    assert any(t["symbol"] == "SPY" and not t["subscribed"] for t in pack["ticker_rows"])


def test_migrate_v1_subscriptions_key(tmp_path: Path):
    p = tmp_path / "old.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "per_group_limit": 40,
                "subscriptions": {"markets.day-watch": True},
            }
        ),
        encoding="utf-8",
    )
    doc = load_subscriptions(p)
    assert doc["sections"].get("markets.day-watch") is True
    saved = save_subscriptions(doc, path=p)
    assert "sections" in saved
    assert saved["sections"].get("markets.day-watch") is True

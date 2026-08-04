"""SA tipsters section subscriptions / snapshots (no live API)."""

from __future__ import annotations

import json
from pathlib import Path

from services.sa_section_subscriptions import (
    SECTION_CATALOG,
    archive_section_snapshot,
    catalog_with_subscriptions,
    list_section_snapshots,
    load_section_snapshot,
    load_subscriptions,
    prune_section_snapshots,
    save_subscriptions,
    set_subscription,
    set_subscriptions_bulk,
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
    assert rows[0]["title"] == "Project Prometheus"
    assert "seekingalpha.com/article/" in rows[0]["link"]
    assert "AI" in rows[0]["summary_text"]

    day = {
        "data": {
            "type": "dayWatch",
            "id": "1",
            "attributes": {
                "top_gainers": [{"id": 1, "slug": "ulh", "name": "Universal Logistics"}],
                "in_the_news": [{"id": 575, "slug": "msft", "name": "Microsoft"}],
            },
        }
    }
    movers = flatten_section_items(day, section_id="markets.day-watch", limit=40, item_kind="day_watch")
    assert len(movers) == 2
    assert movers[0]["tickers"] == ["ULH"] or movers[1]["tickers"] == ["ULH"]
    assert any(x["id"].startswith("in_the_news:") for x in movers)


def test_subscribe_and_snapshot_archive(tmp_path: Path, monkeypatch):
    import services.sa_section_subscriptions as m

    subs = tmp_path / "subs.json"
    latest = tmp_path / "latest.json"
    arch = tmp_path / "snaps"
    monkeypatch.setattr(m, "DEFAULT_SUBS_PATH", subs)
    monkeypatch.setattr(m, "DEFAULT_LATEST_PATH", latest)
    monkeypatch.setattr(m, "DEFAULT_SNAPSHOTS_DIR", arch)
    monkeypatch.setattr(m, "get_config_value", lambda k, d=None: d)

    avail = [c["id"] for c in SECTION_CATALOG if c.get("available")]
    assert "articles.market-outlook" in avail

    set_subscription("articles.market-outlook", True, path=subs)
    doc = load_subscriptions(subs)
    assert doc["subscriptions"].get("articles.market-outlook") is True

    # unavailable cannot enable
    try:
        set_subscription("news.economy", True, path=subs)
        assert False, "expected ValueError"
    except ValueError:
        pass

    set_subscriptions_bulk(
        {"articles.latest-articles": True, "articles.market-outlook": False},
        per_group_limit=25,
        path=subs,
    )
    doc2 = load_subscriptions(subs)
    assert doc2["per_group_limit"] == 25
    assert doc2["subscriptions"].get("articles.latest-articles") is True
    assert "articles.market-outlook" not in doc2["subscriptions"]

    payload = {
        "schema_version": 1,
        "generated_at_utc": "2026-08-04T09:00:00+00:00",
        "per_group_limit": 25,
        "item_count": 2,
        "groups": {
            "articles.latest-articles": {
                "status": "ok",
                "count": 2,
                "items": [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}],
            }
        },
    }
    p = archive_section_snapshot(payload, directory=arch)
    assert p.is_file()
    rows = list_section_snapshots(directory=arch, latest=latest, limit=10)
    assert any(r["id"] == p.stem for r in rows)

    loaded = load_section_snapshot(p.stem, directory=arch, latest=latest)
    assert loaded is not None
    assert loaded["groups"]["articles.latest-articles"]["count"] == 2

    # latest file meta
    latest.write_text(json.dumps(payload), encoding="utf-8")
    pack = catalog_with_subscriptions(subs_path=subs)
    # monkeypatch DEFAULT_LATEST for catalog_with_subscriptions → load_latest_snapshot
    monkeypatch.setattr(m, "DEFAULT_LATEST_PATH", latest)
    pack = catalog_with_subscriptions(subs_path=subs)
    assert pack["per_group_limit"] == 25
    assert any(c["id"] == "articles.latest-articles" and c["subscribed"] for c in pack["catalog"])

    old = arch / "20200101T000000Z.json"
    old.write_text(json.dumps(payload), encoding="utf-8")
    import os
    import time

    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    removed = prune_section_snapshots(retain_days=14, directory=arch)
    assert removed >= 1
    assert not old.exists()


def test_save_subscriptions_roundtrip(tmp_path: Path):
    p = tmp_path / "s.json"
    doc = save_subscriptions(
        {"per_group_limit": 40, "subscriptions": {"articles.stock-ideas": True}},
        path=p,
    )
    assert p.is_file()
    assert doc["subscriptions"]["articles.stock-ideas"] is True
    loaded = load_subscriptions(p)
    assert loaded["subscriptions"]["articles.stock-ideas"] is True

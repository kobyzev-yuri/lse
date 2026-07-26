"""Unit smoke for SA Finance helper (no live RapidAPI call)."""

from __future__ import annotations

from scripts.seeking_alpha_finance_smoke import summarize_items


def test_summarize_items_extracts_titles():
    payload = {
        "data": [
            {
                "id": "1",
                "type": "news",
                "attributes": {
                    "publishOn": "2026-07-26T10:00:00-04:00",
                    "title": "Microsoft headline",
                },
            }
        ]
    }
    rows = summarize_items(payload)
    assert len(rows) == 1
    assert rows[0]["title"] == "Microsoft headline"
    assert rows[0]["id"] == "1"

"""Tests for earnings event freshness windows."""
from __future__ import annotations

from datetime import date

from services.earnings_event_freshness import (
    enrich_event_freshness,
    is_telegram_eligible_event,
    pick_default_active_event,
)


def test_telegram_eligible_within_window():
    today = date(2026, 6, 17)
    assert is_telegram_eligible_event(date(2026, 6, 10), today=today, max_age_days=21)
    assert not is_telegram_eligible_event(date(2026, 5, 1), today=today, max_age_days=21)
    assert not is_telegram_eligible_event(date(2026, 6, 20), today=today, max_age_days=21)


def test_pick_default_active_event_prefers_fresh_llm():
    events = [
        {"symbol": "ORCL", "event_date": "2026-06-10", "has_llm": True, "has_materials": True, "group": "GAME_5M"},
        {"symbol": "NVDA", "event_date": "2026-05-28", "has_llm": True, "group": "GAME_5M"},
    ]
    sym, ev = pick_default_active_event(events)
    assert sym == "ORCL"
    assert ev == "2026-06-10"


def test_enrich_event_freshness_flags():
    today = date(2026, 6, 17)
    rows = enrich_event_freshness(
        [
            {"symbol": "A", "event_date": "2026-06-10"},
            {"symbol": "A", "event_date": "2026-05-01"},
        ],
        today=today,
    )
    assert rows[0]["is_latest_for_symbol"] is True
    assert rows[0]["telegram_eligible"] is True
    assert rows[1]["is_latest_for_symbol"] is False
    assert rows[1]["telegram_eligible"] is False

from datetime import date

from services.earnings_calendar_new_events import (
    brand_new_event_keys,
    load_materials_pipeline_calendar_events,
    pending_event_keys,
)


def test_pending_event_keys():
    events = [
        {"symbol": "meta", "event_date": date(2026, 4, 29), "is_brand_new": True},
        {"symbol": "NVDA", "event_date": date(2026, 5, 28), "is_brand_new": False},
    ]
    assert pending_event_keys(events) == {("META", date(2026, 4, 29)), ("NVDA", date(2026, 5, 28))}
    assert brand_new_event_keys(events) == {("META", date(2026, 4, 29))}


def test_load_materials_pipeline_calendar_events_merges(monkeypatch):
    pending = [
        {
            "symbol": "META",
            "event_date": date(2026, 4, 29),
            "is_brand_new": False,
            "pipeline_reason": "pending_extract",
        },
    ]
    enrich = [
        {
            "symbol": "AMZN",
            "event_date": date(2026, 4, 29),
            "is_brand_new": False,
            "pipeline_reason": "missing_rich_material",
        },
    ]

    def fake_pending(*_a, **_k):
        return pending

    def fake_enrich(*_a, **_k):
        return enrich

    monkeypatch.setattr(
        "services.earnings_calendar_new_events.load_pending_calendar_events",
        fake_pending,
    )
    monkeypatch.setattr(
        "services.earnings_calendar_new_events.load_events_missing_rich_materials",
        fake_enrich,
    )
    merged = load_materials_pipeline_calendar_events(None, since=date(2026, 1, 1), limit=10)
    assert pending_event_keys(merged) == {
        ("META", date(2026, 4, 29)),
        ("AMZN", date(2026, 4, 29)),
    }

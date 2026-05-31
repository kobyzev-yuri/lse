from datetime import date

from services.earnings_calendar_new_events import brand_new_event_keys, pending_event_keys


def test_pending_event_keys():
    events = [
        {"symbol": "meta", "event_date": date(2026, 4, 29), "is_brand_new": True},
        {"symbol": "NVDA", "event_date": date(2026, 5, 28), "is_brand_new": False},
    ]
    assert pending_event_keys(events) == {("META", date(2026, 4, 29)), ("NVDA", date(2026, 5, 28))}
    assert brand_new_event_keys(events) == {("META", date(2026, 4, 29))}

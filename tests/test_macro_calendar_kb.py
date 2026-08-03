"""Unit tests for KB-aligned macro calendar (FRED/FOMC/Investing)."""

from __future__ import annotations

from datetime import date, datetime

from services.kb_extended_fields import (
    investing_calendar_external_id,
    is_macro_calendar_kb_source,
    macro_calendar_external_id,
    macro_calendar_source_label,
)
from services.macro_events_calendar import MacroEvent, _kb_row_to_macro_event, pd_to_date
from services.official_macro_calendar_kb import (
    _event_type_from_fred_title,
    fomc_meetings_to_kb_events,
    fred_releases_to_kb_events,
)


def test_investing_external_id_stable_inv_cal_prefix():
    dt = datetime(2026, 8, 1, 13, 30)
    a = investing_calendar_external_id("USA", dt, "CPI", "CPI")
    b = macro_calendar_external_id("investing", "USA", dt, "CPI", "CPI")
    assert a == b
    assert len(a) == 64


def test_fred_fomc_external_ids_differ_from_investing():
    dt = datetime(2026, 8, 1, 13, 30)
    inv = macro_calendar_external_id("investing", "USA", dt, "CPI", "CPI")
    fred = macro_calendar_external_id("fred", "USA", dt, "CPI", "CPI")
    fomc = macro_calendar_external_id("fomc", "USA", dt, "FOMC decision", "RATE_DECISION")
    assert inv != fred != fomc
    assert inv != fomc


def test_is_macro_calendar_kb_source():
    assert is_macro_calendar_kb_source("Investing.com Economic Calendar (USA)")
    assert is_macro_calendar_kb_source("FRED Economic Calendar (USA)")
    assert is_macro_calendar_kb_source("FOMC Calendar (USA)")
    assert not is_macro_calendar_kb_source("NewsAPI")
    assert not is_macro_calendar_kb_source("")


def test_macro_calendar_source_labels():
    assert macro_calendar_source_label("fred", "USA") == "FRED Economic Calendar (USA)"
    assert macro_calendar_source_label("fomc", "EU") == "FOMC Calendar (USA)"
    assert "Investing.com" in macro_calendar_source_label("investing", "UK")


def test_event_type_from_fred_title():
    assert _event_type_from_fred_title("CPI") == "CPI"
    assert _event_type_from_fred_title("NFP") == "NFP"
    assert _event_type_from_fred_title("Jobless Claims") == "UNEMPLOYMENT"
    assert _event_type_from_fred_title("Retail Sales") == "RETAIL_SALES"


def test_fred_releases_to_kb_events():
    releases = [
        MacroEvent(date=date(2026, 8, 5), kind="economic", title="CPI release", source="fred"),
        MacroEvent(date=date(2026, 8, 6), kind="economic", title="NFP release", source="fred"),
    ]
    rows = fred_releases_to_kb_events(releases)
    assert len(rows) == 2
    assert rows[0]["provider"] == "fred"
    assert rows[0]["event"] == "CPI"
    assert rows[0]["importance"] == "HIGH"
    assert rows[0]["event_date"].hour == 13 and rows[0]["event_date"].minute == 30
    assert rows[0]["source"] == "FRED Economic Calendar (USA)"


def test_fomc_meetings_to_kb_events():
    meetings = [
        {
            "start": "2026-09-15",
            "end": "2026-09-16",
            "decision_date": "2026-09-16",
            "sep": True,
        }
    ]
    rows = fomc_meetings_to_kb_events(meetings)
    assert len(rows) == 1
    assert rows[0]["provider"] == "fomc"
    assert rows[0]["event_type"] == "RATE_DECISION"
    assert "SEP" in rows[0]["event"]
    assert rows[0]["event_date"].hour == 18


def test_kb_row_to_macro_event():
    fred = _kb_row_to_macro_event(
        {
            "ts": datetime(2026, 8, 5, 13, 30),
            "source": "FRED Economic Calendar (USA)",
            "content": "CPI",
            "event_type": "CPI",
        }
    )
    assert fred is not None
    assert fred.kind == "economic"
    assert fred.source == "kb:fred"
    assert fred.date == date(2026, 8, 5)

    fomc = _kb_row_to_macro_event(
        {
            "ts": datetime(2026, 9, 16, 18, 0),
            "source": "FOMC Calendar (USA)",
            "content": "FOMC decision (SEP / Dot Plot)",
            "event_type": "RATE_DECISION",
        }
    )
    assert fomc is not None
    assert fomc.kind == "fomc"
    assert fomc.source == "kb:fomc"


def test_pd_to_date():
    assert pd_to_date(datetime(2026, 8, 1, 12, 0)) == date(2026, 8, 1)
    assert pd_to_date("2026-08-01 13:30:00") == date(2026, 8, 1)

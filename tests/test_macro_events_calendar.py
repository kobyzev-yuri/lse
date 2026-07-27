"""Tests for FOMC calendar parser (Fed.gov HTML port)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from services.macro_events_calendar import (
    fomc_to_events,
    parse_fomc_calendar,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fomc_calendar_sample.html"


def test_parse_fomc_calendar_fixture():
    body = FIXTURE.read_bytes()
    meetings = parse_fomc_calendar(body)
    assert len(meetings) >= 40
    # Known SEP meeting in sample era
    sep = [m for m in meetings if m["sep"]]
    assert sep, "expected at least one SEP (*) meeting"
    for m in meetings:
        assert m["start"] <= m["end"]
        date.fromisoformat(m["decision_date"])


def test_fomc_to_events_titles():
    events = fomc_to_events(
        [
            {
                "start": "2026-07-28",
                "end": "2026-07-29",
                "decision_date": "2026-07-29",
                "sep": False,
            },
            {
                "start": "2026-09-16",
                "end": "2026-09-17",
                "decision_date": "2026-09-17",
                "sep": True,
            },
        ]
    )
    assert events[0].kind == "fomc"
    assert events[0].title == "FOMC decision"
    assert "SEP" in events[1].title
    assert events[0].to_dict()["date"] == "2026-07-29"

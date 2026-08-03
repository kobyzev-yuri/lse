"""
FRED + FOMC → knowledge_base (same event contract as Investing calendar).

KB is the shared store for LSE features/gates and notebook calendar.
Live FRED/FOMC in macro_events_calendar remains UI fallback only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

from services.kb_extended_fields import macro_calendar_source_label
from services.macro_events_calendar import (
    fetch_fomc_meetings,
    fetch_fred_releases,
)

logger = logging.getLogger(__name__)

# US macro release convention for proximity features (approx. 8:30 ET ≈ 13:30 UTC).
_FRED_RELEASE_UTC = time(13, 30)
# FOMC statement typically ~14:00 ET ≈ 18:00 UTC.
_FOMC_DECISION_UTC = time(18, 0)


def _event_type_from_fred_title(title: str) -> str:
    t = (title or "").lower()
    if "cpi" in t:
        return "CPI"
    if "ppi" in t:
        return "PPI"
    if "nfp" in t or "nonfarm" in t or "non-farm" in t or "payroll" in t:
        return "NFP"
    if "pce" in t:
        return "PCE"
    if "retail" in t:
        return "RETAIL_SALES"
    if "gdp" in t:
        return "GDP"
    if "jobless" in t or "unemployment" in t or "claims" in t:
        return "UNEMPLOYMENT"
    if "durable" in t:
        return "DURABLE_GOODS"
    return "ECONOMIC_INDICATOR"


def _naive_utc(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def fred_releases_to_kb_events(
    releases: Optional[List[Any]] = None,
    *,
    days_ahead: int = 45,
) -> List[Dict[str, Any]]:
    """Map MacroEvent FRED rows → save_events_to_db dicts."""
    from services.macro_events_calendar import MacroEvent

    items = releases if releases is not None else fetch_fred_releases(days_ahead=days_ahead)
    out: List[Dict[str, Any]] = []
    for ev in items:
        if not isinstance(ev, MacroEvent):
            continue
        if ev.kind != "economic":
            continue
        name = (ev.title or "").strip()
        if not name:
            continue
        # Strip trailing " release" for cleaner content; keep title as event name.
        event_name = name[:-8].strip() if name.lower().endswith(" release") else name
        event_type = _event_type_from_fred_title(event_name)
        out.append(
            {
                "event": event_name,
                "event_date": _naive_utc(ev.date, _FRED_RELEASE_UTC),
                "event_type": event_type,
                "region": "USA",
                "importance": "HIGH",
                "provider": "fred",
                "source": macro_calendar_source_label("fred", "USA"),
                "forecast": None,
                "previous": None,
                "actual": None,
            }
        )
    return out


def fomc_meetings_to_kb_events(
    meetings: Optional[List[Dict[str, Any]]] = None,
    *,
    days_ahead: int = 120,
) -> List[Dict[str, Any]]:
    """Map FOMC meeting dicts → save_events_to_db dicts."""
    raw = meetings if meetings is not None else fetch_fomc_meetings(
        days_ahead=days_ahead, include_past_days=0
    )
    out: List[Dict[str, Any]] = []
    for m in raw:
        try:
            d = date.fromisoformat(str(m.get("decision_date") or m.get("end")))
        except (TypeError, ValueError):
            continue
        title = "FOMC decision"
        if m.get("sep"):
            title += " (SEP / Dot Plot)"
        out.append(
            {
                "event": title,
                "event_date": _naive_utc(d, _FOMC_DECISION_UTC),
                "event_type": "RATE_DECISION",
                "region": "USA",
                "importance": "HIGH",
                "provider": "fomc",
                "source": macro_calendar_source_label("fomc", "USA"),
                "forecast": None,
                "previous": None,
                "actual": None,
            }
        )
    return out


def fetch_and_save_official_macro_calendar(
    *,
    fred_days_ahead: int = 45,
    fomc_days_ahead: int = 120,
) -> Tuple[int, int]:
    """
    Fetch FRED releases + FOMC meetings and upsert into knowledge_base.
    Returns (n_events, n_saved_new).
    """
    from services.investing_calendar_parser import save_events_to_db

    events: List[Dict[str, Any]] = []
    try:
        fred_ev = fred_releases_to_kb_events(days_ahead=fred_days_ahead)
        events.extend(fred_ev)
        logger.info("Official macro calendar: FRED → %s events", len(fred_ev))
    except Exception as e:
        logger.warning("Official macro calendar: FRED failed: %s", e)

    try:
        fomc_ev = fomc_meetings_to_kb_events(days_ahead=fomc_days_ahead)
        events.extend(fomc_ev)
        logger.info("Official macro calendar: FOMC → %s events", len(fomc_ev))
    except Exception as e:
        logger.warning("Official macro calendar: FOMC failed: %s", e)

    if not events:
        logger.info("Official macro calendar: no events to save (missing FRED key or empty window)")
        return 0, 0

    saved = save_events_to_db(events)
    logger.info(
        "Official macro calendar: saved %s new KB rows from %s events",
        saved,
        len(events),
    )
    return len(events), saved

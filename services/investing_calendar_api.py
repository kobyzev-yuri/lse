"""
Экономический календарь Investing.com через JSON API (как nyse/sources/ecalendar.py),
без разбора HTML-таблицы.

Эндпоинт: endpoints.investing.com/.../economic/events/occurrences
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from services.http_outbound import outbound_session

logger = logging.getLogger(__name__)

ENDPOINT = (
    "https://endpoints.investing.com/pd-instruments/v1/calendars/economic/events/occurrences"
)

# country_id Investing → ключ region как в services.investing_calendar_parser.REGIONS (для US_MACRO / MACRO)
API_COUNTRY_ID_TO_REGION: Dict[int, str] = {
    5: "USA",  # United States
    4: "UK",  # United Kingdom
    17: "EU",  # Germany → зона EU в LSE
    72: "EU",  # Euro Zone
    22: "EU",  # France
    10: "EU",  # Italy
    35: "Japan",
    37: "China",
    12: "Switzerland",
}

INVESTING_CALENDAR_API_BACKOFF = (0, 4, 12)


def _event_type_from_name(name: str) -> str:
    event_lower = (name or "").lower()
    if "rate" in event_lower and "decision" in event_lower:
        return "RATE_DECISION"
    if "cpi" in event_lower or "inflation" in event_lower:
        return "CPI"
    if "ppi" in event_lower:
        return "PPI"
    if "nfp" in event_lower or "non-farm payrolls" in event_lower:
        return "NFP"
    if "pmi" in event_lower:
        return "PMI"
    if "gdp" in event_lower:
        return "GDP"
    if "unemployment" in event_lower:
        return "UNEMPLOYMENT"
    if "retail sales" in event_lower:
        return "RETAIL_SALES"
    return "ECONOMIC_INDICATOR"


def _importance_to_bucket(raw: Any) -> Optional[str]:
    s = str(raw or "").strip().lower()
    if not s or "low" in s or "holiday" in s:
        return None
    if "high" in s:
        return "HIGH"
    if "medium" in s or "moderate" in s:
        return "MEDIUM"
    return "MEDIUM"


def _parse_occurrence_time(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _format_value(value: Any, unit: Any) -> Optional[str]:
    if value is None:
        return None
    if unit:
        try:
            return f"{value}{unit}"
        except Exception:
            return str(value)
    return str(value)


def _get_json_with_retries(session: requests.Session, params: dict) -> dict:
    for attempt, delay_sec in enumerate(INVESTING_CALENDAR_API_BACKOFF):
        if delay_sec:
            time.sleep(delay_sec)
        try:
            r = session.get(ENDPOINT, params=params, timeout=25)
            if r.status_code == 429 and attempt < len(INVESTING_CALENDAR_API_BACKOFF) - 1:
                logger.warning(
                    "Investing calendar API 429, retry %s/%s",
                    attempt + 1,
                    len(INVESTING_CALENDAR_API_BACKOFF),
                )
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt < len(INVESTING_CALENDAR_API_BACKOFF) - 1:
                logger.warning("Investing calendar API request failed (%s), retrying", e)
                continue
            raise
    raise RuntimeError("investing calendar API: empty backoff configuration")


def fetch_investing_calendar_api_events(
    *,
    days_back: int = 2,
    days_forward: int = 3,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """
    Возвращает те же dict, что ожидает save_events_to_db (как HTML-парсер):
    time, currency, importance, event, actual, forecast, previous, region, event_type, event_date,
    плюс api_event_id для трассировки в raw_payload.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=int(days_back))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = (now + timedelta(days=int(days_forward))).replace(
        hour=23, minute=59, second=59, microsecond=999000
    )
    country_ids = ",".join(str(i) for i in sorted(API_COUNTRY_ID_TO_REGION.keys()))
    base_params: Dict[str, Any] = {
        "domain_id": 1,
        "limit": int(limit),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "country_ids": country_ids,
    }

    sess = outbound_session("INVESTING_CALENDAR_USE_SYSTEM_PROXY")
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://www.investing.com/economic-calendar/",
            "Origin": "https://www.investing.com",
        }
    )

    events_by_id: Dict[Any, Dict[str, Any]] = {}
    all_occurrences: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params = dict(base_params)
        if cursor:
            params["cursor"] = cursor
        data = _get_json_with_retries(sess, params)
        for e in data.get("events", []) or []:
            eid = e.get("event_id")
            if eid is not None:
                events_by_id[eid] = e
        all_occurrences.extend(data.get("occurrences", []) or [])
        cursor = data.get("next_page_cursor")
        if not cursor:
            break

    out: List[Dict[str, Any]] = []
    for occ in all_occurrences:
        meta = events_by_id.get(occ.get("event_id"))
        if not meta:
            continue
        imp_bucket = _importance_to_bucket(meta.get("importance"))
        if imp_bucket is None:
            continue
        cid = meta.get("country_id")
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            continue
        region = API_COUNTRY_ID_TO_REGION.get(cid_int)
        if region is None:
            continue
        event_dt = _parse_occurrence_time(occ.get("occurrence_time"))
        if event_dt is None:
            continue
        name = str(meta.get("short_name") or meta.get("name") or "").strip()
        if not name:
            continue
        unit = occ.get("unit")
        event = {
            "time": (occ.get("occurrence_time") or "")[:32],
            "currency": str(meta.get("currency") or ""),
            "importance": imp_bucket,
            "event": name,
            "actual": _format_value(occ.get("actual"), unit),
            "forecast": _format_value(occ.get("forecast"), unit),
            "previous": _format_value(occ.get("previous"), unit),
            "region": region,
            "event_type": _event_type_from_name(name),
            "event_date": event_dt,
            "api_event_id": meta.get("event_id"),
            "api_occurrence_time": occ.get("occurrence_time"),
        }
        out.append(event)

    out.sort(key=lambda e: e["event_date"])
    logger.info("Investing calendar API: %s событий (после фильтра регион/важность)", len(out))
    return out

"""Macro events calendar: FOMC (Fed.gov) + FRED releases + Yahoo earnings.

Python port of wintermonth2298/marketdata calendar adapters (Go), for LSE:

- FOMC: scrape official Fed meeting calendar (stable, no API key).
- FRED: release dates for CPI/PPI/NFP/… (needs FRED_API_KEY).
- Earnings: next report via yfinance (Yahoo), for notebook tickers.

Primary economic calendar in LSE remains Investing.com → knowledge_base.
This module is:
1) live FOMC for notebook Verdict (ФРС row),
2) fallback / secondary feed when Investing is empty or for UI calendar panel.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED_BASE = "https://api.stlouisfed.org/fred/"

# FRED release_id → short title (same IDs as Go pkg/fred)
FRED_INDICATORS: Dict[int, str] = {
    10: "CPI",
    46: "PPI",
    50: "NFP",
    54: "PCE",
    9: "Retail Sales",
    53: "GDP",
    95: "Durable Goods",
    180: "Jobless Claims",
}

_YEAR_RE = re.compile(r"^(\d{4}) FOMC Meetings$")
_DATE_RE = re.compile(r"^(\d{1,2})(?:-(\d{1,2}))?(\*)?(?:\s*\(([^)]*)\))?$")
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL_SEC = 3600.0


def _cache_get(key: str) -> Any:
    hit = _CACHE.get(key)
    if not hit:
        return None
    at, val = hit
    if time.time() - at > _CACHE_TTL_SEC:
        return None
    return val


def _cache_put(key: str, val: Any) -> Any:
    _CACHE[key] = (time.time(), val)
    return val


@dataclass
class MacroEvent:
    date: date
    kind: str  # economic | fomc | earnings
    title: str
    source: str
    symbol: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "kind": self.kind,
            "title": self.title,
            "source": self.source,
            "symbol": self.symbol or "",
        }


# ---- HTML text lines (Fed calendar) ----


class _TextLinesExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        s = " ".join((data or "").split())
        if s:
            self.lines.append(s)


def html_text_lines(body: bytes | str) -> List[str]:
    raw = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    p = _TextLinesExtractor()
    p.feed(raw)
    return p.lines


def _parse_month(s: str) -> Optional[int]:
    return _MONTHS.get(str(s or "").strip().lower())


def _is_month_line(s: str) -> bool:
    parts = [p.strip() for p in str(s).split("/")]
    if not parts or len(parts) > 2:
        return False
    return all(_parse_month(p) is not None for p in parts)


def _months_for_range(month_line: str, start_day: int, end_day: int) -> Tuple[int, int]:
    parts = [p.strip() for p in month_line.split("/")]
    start_m = _parse_month(parts[0])
    if not start_m:
        raise ValueError(f"unknown month {parts[0]!r}")
    end_m = start_m
    if len(parts) == 2:
        end_m = _parse_month(parts[1])
        if not end_m:
            raise ValueError(f"unknown month {parts[1]!r}")
    elif end_day < start_day:
        end_m = start_m + 1 if start_m < 12 else 1
    return start_m, end_m


def parse_fomc_calendar(body: bytes | str) -> List[Dict[str, Any]]:
    """Parse Fed FOMC calendar HTML → list of meetings {start, end, sep, note}."""
    lines = html_text_lines(body)
    year = 0
    month = ""
    meetings: List[Dict[str, Any]] = []

    for line in lines:
        ym = _YEAR_RE.match(line)
        if ym:
            year = int(ym.group(1))
            month = ""
            continue
        if year == 0:
            continue
        if _is_month_line(line):
            month = line
            continue
        if not month:
            continue
        dm = _DATE_RE.match(line)
        if not dm:
            continue
        note = (dm.group(4) or "").strip()
        if "notation vote" in note.lower():
            continue
        start_day = int(dm.group(1))
        end_day = int(dm.group(2)) if dm.group(2) else start_day
        start_m, end_m = _months_for_range(month, start_day, end_day)
        start = date(year, start_m, start_day)
        end_year = year + 1 if end_m < start_m else year
        end = date(end_year, end_m, end_day)
        meetings.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "decision_date": end.isoformat(),
                "sep": dm.group(3) == "*",
                "note": note,
            }
        )

    if not meetings:
        raise ValueError("no FOMC meetings parsed")
    meetings.sort(key=lambda m: m["start"])
    return meetings


def fetch_fomc_meetings(
    *,
    days_ahead: int = 120,
    include_past_days: int = 7,
    timeout: float = 30.0,
    body: Optional[bytes] = None,
) -> List[Dict[str, Any]]:
    """Fetch + filter FOMC meetings around today."""
    cache_key = f"fomc:{days_ahead}:{include_past_days}"
    if body is None:
        cached = _cache_get(cache_key)
        if cached is not None:
            return list(cached)

    if body is None:
        req = urllib.request.Request(
            FOMC_CALENDAR_URL,
            headers={"User-Agent": "lse-macro-calendar/1.0", "Accept": "text/html"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()

    meetings = parse_fomc_calendar(body)
    today = datetime.now(timezone.utc).date()
    lo = today - timedelta(days=max(0, int(include_past_days)))
    hi = today + timedelta(days=max(1, int(days_ahead)))
    out = []
    for m in meetings:
        end = date.fromisoformat(m["end"])
        start = date.fromisoformat(m["start"])
        if end < lo or start > hi:
            continue
        out.append(m)
    if body is not None and cache_key:
        _cache_put(cache_key, out)
    return out


def next_fomc_decision(*, days_ahead: int = 120) -> Optional[Dict[str, Any]]:
    """Next FOMC decision date (meeting end) on/after today."""
    today = datetime.now(timezone.utc).date()
    try:
        meetings = fetch_fomc_meetings(days_ahead=days_ahead, include_past_days=0)
    except Exception as e:
        logger.warning("FOMC fetch failed: %s", e)
        return None
    upcoming = [m for m in meetings if date.fromisoformat(m["decision_date"]) >= today]
    if not upcoming:
        return None
    m = upcoming[0]
    d = date.fromisoformat(m["decision_date"])
    days = (d - today).days
    title = "FOMC decision"
    if m.get("sep"):
        title += " (SEP / Dot Plot)"
    return {
        "date": d.isoformat(),
        "days_until": days,
        "title": title,
        "sep": bool(m.get("sep")),
        "source": "fomc",
        "url": FOMC_CALENDAR_URL,
    }


def fomc_env_snapshot() -> Optional[Dict[str, Any]]:
    """Notebook Environment Check row for ФРС from official FOMC calendar."""
    nxt = next_fomc_decision()
    if not nxt:
        return {
            "lbl": "Риторика ФРС",
            "st": "FOMC календарь пуст / недоступен",
            "state": "mid",
            "live": True,
            "source": "live · federalreserve.gov",
            "metric": "fed",
        }
    days = int(nxt["days_until"])
    title = nxt["title"]
    if days <= 1:
        state, st = "bad", f"сегодня/завтра · {title} ({nxt['date']})"
    elif days <= 3:
        state, st = "mid", f"через {days}д · {title} ({nxt['date']})"
    elif days <= 10:
        state, st = "mid", f"через {days}д · {title} ({nxt['date']})"
    else:
        state, st = "ok", f"след. {title} {nxt['date']} · через {days}д"
    return {
        "lbl": "Риторика ФРС",
        "st": st,
        "state": state,
        "live": True,
        "source": "live · federalreserve.gov FOMC",
        "metric": "fed",
        "fomc": nxt,
    }


# ---- FRED release calendar ----


def _fred_api_key() -> str:
    try:
        from config_loader import get_config_value

        return (
            (get_config_value("FRED_API_KEY") or "").strip()
            or (get_config_value("FRED_KEY") or "").strip()
        )
    except Exception:
        return ""


def fetch_fred_releases(
    *,
    days_ahead: int = 30,
    indicators: Optional[Dict[int, str]] = None,
    timeout: float = 25.0,
) -> List[MacroEvent]:
    key = _fred_api_key()
    if not key:
        return []
    inds = indicators or FRED_INDICATORS
    today = datetime.now(timezone.utc).date()
    to = today + timedelta(days=max(1, int(days_ahead)))
    cache_key = f"fred:{today.isoformat()}:{to.isoformat()}:{','.join(map(str, sorted(inds)))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return list(cached)

    out: List[MacroEvent] = []
    for rid, title in inds.items():
        params = urllib.parse.urlencode(
            {
                "release_id": str(rid),
                "api_key": key,
                "file_type": "json",
                "sort_order": "asc",
                "include_release_dates_with_no_data": "true",
                "realtime_start": today.isoformat(),
                "realtime_end": to.isoformat(),
            }
        )
        url = f"{FRED_BASE}release/dates?{params}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8", errors="replace"))
            for rd in raw.get("release_dates") or []:
                ds = str(rd.get("date") or "")
                try:
                    d = date.fromisoformat(ds)
                except ValueError:
                    continue
                if d < today or d > to:
                    continue
                out.append(
                    MacroEvent(
                        date=d,
                        kind="economic",
                        title=f"{title} release",
                        source="fred",
                    )
                )
        except Exception as e:
            logger.warning("FRED release_id=%s failed: %s", rid, e)

    out.sort(key=lambda e: (e.date, e.title))
    return _cache_put(cache_key, out)


# ---- Yahoo / yfinance earnings ----


def fetch_yahoo_earnings_events(
    symbols: Sequence[str],
    *,
    days_ahead: int = 30,
    max_workers: int = 8,
) -> List[MacroEvent]:
    """Next earnings date per symbol via yfinance (within window)."""
    today = datetime.now(timezone.utc).date()
    hi = today + timedelta(days=max(1, int(days_ahead)))
    wanted = []
    seen = set()
    for s in symbols:
        u = str(s or "").strip().upper()
        if not u or u.startswith("^") or u in seen:
            continue
        seen.add(u)
        wanted.append(u)
    if not wanted:
        return []

    try:
        import yfinance as yf
    except Exception as e:
        logger.warning("yfinance missing for earnings calendar: %s", e)
        return []

    def _one(sym: str) -> Optional[MacroEvent]:
        try:
            t = yf.Ticker(sym)
            # calendar may be dict or DataFrame depending on yfinance version
            cal = None
            try:
                cal = t.calendar
            except Exception:
                cal = None
            ed: Optional[date] = None
            when = ""
            if isinstance(cal, dict):
                raw = cal.get("Earnings Date") or cal.get("earningsDate")
                if isinstance(raw, (list, tuple)) and raw:
                    raw0 = raw[0]
                    if hasattr(raw0, "date"):
                        ed = raw0.date()
                    elif isinstance(raw0, datetime):
                        ed = raw0.date()
                    elif isinstance(raw0, date):
                        ed = raw0
                elif hasattr(raw, "date"):
                    ed = raw.date()
            if ed is None:
                try:
                    edf = t.get_earnings_dates(limit=4)
                    if edf is not None and len(edf.index):
                        for idx in edf.index:
                            if hasattr(idx, "date"):
                                d0 = idx.date()
                            elif isinstance(idx, datetime):
                                d0 = idx.date()
                            else:
                                continue
                            if d0 >= today:
                                ed = d0
                                break
                except Exception:
                    pass
            if ed is None or ed < today or ed > hi:
                return None
            title = f"{sym} earnings"
            if when:
                title += when
            return MacroEvent(date=ed, kind="earnings", title=title, source="yahoo", symbol=sym)
        except Exception as e:
            logger.debug("earnings %s: %s", sym, e)
            return None

    out: List[MacroEvent] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(wanted)))) as pool:
        futs = {pool.submit(_one, s): s for s in wanted}
        for fut in as_completed(futs):
            ev = fut.result()
            if ev:
                out.append(ev)
    out.sort(key=lambda e: (e.date, e.symbol or e.title))
    return out


def fomc_to_events(meetings: Iterable[Dict[str, Any]]) -> List[MacroEvent]:
    out: List[MacroEvent] = []
    for m in meetings:
        d = date.fromisoformat(str(m.get("decision_date") or m.get("end")))
        title = "FOMC decision"
        if m.get("sep"):
            title += " (SEP / Dot Plot)"
        out.append(MacroEvent(date=d, kind="fomc", title=title, source="fomc"))
    return out


def build_macro_events(
    *,
    days: int = 21,
    symbols: Optional[Sequence[str]] = None,
    include_fred: bool = True,
    include_fomc: bool = True,
    include_earnings: bool = True,
) -> Dict[str, Any]:
    """Merged calendar for UI (same shape as Go Events list / calendar.html)."""
    days = max(1, min(int(days), 90))
    events: List[MacroEvent] = []
    errors: Dict[str, str] = {}

    if include_fomc:
        try:
            meetings = fetch_fomc_meetings(days_ahead=days, include_past_days=0)
            events.extend(fomc_to_events(meetings))
        except Exception as e:
            errors["fomc"] = str(e)[:200]
            logger.warning("build_macro_events FOMC: %s", e)

    if include_fred:
        try:
            events.extend(fetch_fred_releases(days_ahead=days))
        except Exception as e:
            errors["fred"] = str(e)[:200]

    if include_earnings and symbols:
        try:
            events.extend(fetch_yahoo_earnings_events(symbols, days_ahead=days))
        except Exception as e:
            errors["yahoo"] = str(e)[:200]

    events.sort(key=lambda e: (e.date, e.title))
    # de-dupe identical date+title+symbol
    seen = set()
    uniq: List[MacroEvent] = []
    for e in events:
        k = (e.date.isoformat(), e.kind, e.title, e.symbol)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)

    return {
        "asof_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "days": days,
        "events": [e.to_dict() for e in uniq],
        "counts": {
            "total": len(uniq),
            "fomc": sum(1 for e in uniq if e.kind == "fomc"),
            "economic": sum(1 for e in uniq if e.kind == "economic"),
            "earnings": sum(1 for e in uniq if e.kind == "earnings"),
        },
        "errors": errors,
        "sources_note": (
            "FOMC=federalreserve.gov; economic=FRED (FRED_API_KEY); "
            "earnings=yfinance. Investing.com remains primary KB ingest."
        ),
    }


def load_fixture_html(path: Optional[Path] = None) -> bytes:
    p = path or (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fomc_calendar_sample.html"
    )
    return p.read_bytes()

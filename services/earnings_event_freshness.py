"""Freshness windows for earnings Telegram alerts and UI calendar defaults."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from config_loader import get_config_value


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def telegram_max_event_age_days() -> int:
    """Max days after event_date for proactive Telegram (brief, digest event block)."""
    return max(1, _cfg_int("EARNINGS_TELEGRAM_MAX_EVENT_AGE_DAYS", 21))


def ui_calendar_window_days() -> int:
    """Default lookback on earnings calendar first page."""
    return max(1, _cfg_int("EARNINGS_UI_CALENDAR_WINDOW_DAYS", 30))


def days_since_event(event_date: date | None, *, today: date | None = None) -> int | None:
    if not isinstance(event_date, date):
        return None
    ref = today or date.today()
    return (ref - event_date).days


def is_telegram_eligible_event(
    event_date: date | None,
    *,
    today: date | None = None,
    max_age_days: int | None = None,
) -> bool:
    """Past event within ops window — skip stale reports in Telegram pushes."""
    if not isinstance(event_date, date):
        return False
    ref = today or date.today()
    if event_date > ref:
        return False
    age = (ref - event_date).days
    limit = max_age_days if max_age_days is not None else telegram_max_event_age_days()
    return age <= limit


def calendar_since_date(*, window_days: int | None = None, today: date | None = None) -> date:
    ref = today or date.today()
    days = window_days if window_days is not None else ui_calendar_window_days()
    return ref - timedelta(days=max(0, days - 1))


def enrich_event_freshness(
    events: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Add days_since_event, telegram_eligible, is_latest_for_symbol."""
    ref = today or date.today()
    latest_by_symbol: dict[str, date] = {}
    for ev in events:
        sym = str(ev.get("symbol") or "").upper()
        ev_d_raw = ev.get("event_date")
        if isinstance(ev_d_raw, str):
            try:
                ev_d = date.fromisoformat(ev_d_raw[:10])
            except ValueError:
                continue
        elif isinstance(ev_d_raw, date):
            ev_d = ev_d_raw
        else:
            continue
        prev = latest_by_symbol.get(sym)
        if prev is None or ev_d > prev:
            latest_by_symbol[sym] = ev_d

    out: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        ev_d_raw = row.get("event_date")
        if isinstance(ev_d_raw, str):
            try:
                ev_d = date.fromisoformat(ev_d_raw[:10])
            except ValueError:
                ev_d = None
        elif isinstance(ev_d_raw, date):
            ev_d = ev_d_raw
            row["event_date"] = ev_d.isoformat()
        else:
            ev_d = None
        age = days_since_event(ev_d, today=ref)
        sym = str(row.get("symbol") or "").upper()
        row["days_since_event"] = age
        row["telegram_eligible"] = is_telegram_eligible_event(ev_d, today=ref)
        row["is_latest_for_symbol"] = bool(
            ev_d and sym and latest_by_symbol.get(sym) == ev_d
        )
        out.append(row)
    return out


def pick_default_active_event(events: list[dict[str, Any]]) -> tuple[str, str] | None:
    """
    Freshest actionable event for UI focus: max event_date, prefer LLM/materials/GAME_5M.
    """
    candidates: list[tuple[date, int, str, str]] = []
    for ev in events:
        sym = str(ev.get("symbol") or "").upper()
        raw = ev.get("event_date")
        if not sym or not raw:
            continue
        try:
            ev_d = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        score = 0
        if ev.get("has_llm"):
            score += 4
        if ev.get("has_materials"):
            score += 2
        if "GAME_5M" in str(ev.get("group") or ""):
            score += 1
        candidates.append((ev_d, score, sym, ev_d.isoformat()))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    _, _, sym, ev_iso = candidates[0]
    return sym, ev_iso

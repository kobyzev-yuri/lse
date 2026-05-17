"""
Shared helpers for multiday LR daily feature ingest (news, macro calendar, symbol calendar).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    NYSE_TZ = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover
    NYSE_TZ = None

NYSE_REGULAR_CLOSE = time(16, 0)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def session_close_utc(trade_d: date) -> datetime:
    """US cash equity regular close 16:00 ET on trade_date -> UTC naive for DB compare."""
    if NYSE_TZ is None:
        raise RuntimeError("zoneinfo required for session_close_utc")
    ts = datetime.combine(trade_d, NYSE_REGULAR_CLOSE, tzinfo=NYSE_TZ)
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def session_day_start_utc(trade_d: date) -> datetime:
    if NYSE_TZ is None:
        raise RuntimeError("zoneinfo required")
    ts = datetime.combine(trade_d, time(0, 0), tzinfo=NYSE_TZ)
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def apply_sql_migrations(engine: Engine, sql_names: Sequence[str]) -> None:
    sql_dir = PROJECT_ROOT / "db" / "knowledge_pg" / "sql"
    for name in sql_names:
        path = sql_dir / name
        ddl = path.read_text(encoding="utf-8")
        lines: List[str] = []
        for line in ddl.splitlines():
            if line.strip().startswith("--"):
                continue
            lines.append(line)
        body = "\n".join(lines)
        parts = re.split(r";\s*\n", body)
        with engine.begin() as conn:
            for part in parts:
                part = part.strip()
                if part:
                    conn.execute(text(part + ";"))


def dedupe_symbols(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        t = str(item or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def resolve_tickers_for_ingest(source: str, engine: Optional[Engine]) -> List[str]:
    src = (source or "game5m").strip().lower()
    if src == "game5m":
        from services.ticker_groups import get_tickers_game_5m

        return dedupe_symbols(get_tickers_game_5m() or [])
    if src == "config":
        from services.ticker_groups import get_config_ticker_symbols_upper_unique

        return list(get_config_ticker_symbols_upper_unique())
    if src == "manual":
        return []
    raise ValueError(f"unknown tickers source: {source!r}")


def trading_dates_from_quotes(engine: Engine, symbol: str, min_date: date, max_date: date) -> List[date]:
    sym = str(symbol).strip().upper()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT date::date AS d
                FROM public.quotes
                WHERE ticker = :sym AND date::date >= :d0 AND date::date <= :d1
                ORDER BY date ASC
                """
            ),
            {"sym": sym, "d0": min_date, "d1": max_date},
        ).fetchall()
    return [r[0] for r in rows if r and r[0] is not None]


def trading_day_offset(dates_sorted: Sequence[date], origin: date, target: date) -> Optional[int]:
    """Trading days from origin to target along dates_sorted (0 if same day)."""
    if target < origin:
        return None
    try:
        i0 = dates_sorted.index(origin)
    except ValueError:
        return None
    if target == origin:
        return 0
    count = 0
    for d in dates_sorted[i0 + 1 :]:
        if d > target:
            break
        count += 1
        if d == target:
            return count
    return None


def parse_date_arg(s: str) -> date:
    return pd.Timestamp(str(s).strip()).date()


def default_date_range(period_days: int) -> Tuple[date, date]:
    period_days = max(30, min(int(period_days), 2000))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=period_days)
    return start, end

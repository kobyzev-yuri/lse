"""KB earnings event_date matching with calendar-day tolerance."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

DEFAULT_EVENT_DATE_TOLERANCE_DAYS = 1


def expand_event_date_keys(
    keys: Iterable[tuple[str, date]],
    *,
    tolerance_days: int = DEFAULT_EVENT_DATE_TOLERANCE_DAYS,
) -> set[tuple[str, date]]:
    """Expand (symbol, kb_event_date) to include nearby material event_date rows."""
    tol = max(0, int(tolerance_days))
    out: set[tuple[str, date]] = set()
    for sym, ev_date in keys:
        sym_u = str(sym or "").strip().upper()
        if not sym_u or not isinstance(ev_date, date):
            continue
        for offset in range(-tol, tol + 1):
            out.add((sym_u, ev_date + timedelta(days=offset)))
    return out


def material_matches_kb_event_date_sql(
    *,
    symbol_expr: str = "UPPER(TRIM(em.symbol))",
    kb_symbol_expr: str = "UPPER(TRIM(kb.ticker))",
    material_date_expr: str = "em.event_date",
    kb_date_expr: str = "kb.ts::date",
    tolerance_days: int = DEFAULT_EVENT_DATE_TOLERANCE_DAYS,
) -> str:
    """SQL fragment: earnings_material row matches KB event within tolerance."""
    tol = max(0, int(tolerance_days))
    if tol == 0:
        return (
            f"{symbol_expr} = {kb_symbol_expr} AND {material_date_expr} = {kb_date_expr}"
        )
    return (
        f"{symbol_expr} = {kb_symbol_expr} "
        f"AND ABS({material_date_expr} - {kb_date_expr}) <= {tol}"
    )


def resolve_kb_id_for_earnings_event(
    engine: Engine,
    *,
    symbol: str,
    event_date: date,
    fallback: int | None = None,
    tolerance_days: int = DEFAULT_EVENT_DATE_TOLERANCE_DAYS,
) -> int | None:
    """Resolve knowledge_base.id for symbol+date; exact match first, then nearest within tolerance."""
    if fallback:
        return int(fallback)
    sym = symbol.strip().upper()
    tol = max(0, int(tolerance_days))

    exact_q = text(
        """
        SELECT id
        FROM knowledge_base
        WHERE UPPER(TRIM(ticker)) = :symbol
          AND ts::date = :event_date
          AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
        ORDER BY id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(exact_q, {"symbol": sym, "event_date": event_date}).first()
        if row:
            return int(row[0])
        if tol <= 0:
            return None
        near_q = text(
            """
            SELECT id
            FROM knowledge_base
            WHERE UPPER(TRIM(ticker)) = :symbol
              AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
              AND ABS(ts::date - :event_date) <= :tol
            ORDER BY ABS(ts::date - :event_date) ASC, id DESC
            LIMIT 1
            """
        )
        row = conn.execute(near_q, {"symbol": sym, "event_date": event_date, "tol": tol}).first()
    return int(row[0]) if row else None

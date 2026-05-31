"""KB earnings calendar events that still need the materials pipeline."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


def load_pending_calendar_events(
    engine: Engine,
    *,
    since: date | None = None,
    symbols: set[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Calendar events without completed LLM extraction (materials pipeline still open).

    Includes brand-new KB rows (zero earnings_material) and in-progress events
    (registered/failed/parsed but no extraction_meta yet).
    """
    where = ["UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'"]
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if since:
        where.append("kb.ts::date >= :since")
        params["since"] = since
    if symbols:
        where.append("UPPER(TRIM(kb.ticker)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    where.append(
        """
        NOT EXISTS (
          SELECT 1
          FROM earnings_event_detail eed
          WHERE eed.knowledge_base_id = kb.id
            AND eed.guidance_summary ? 'extraction_meta'
        )
        """
    )
    q = text(
        f"""
        SELECT
          kb.id AS knowledge_base_id,
          UPPER(TRIM(kb.ticker)) AS symbol,
          kb.ts::date AS event_date,
          NOT EXISTS (
            SELECT 1
            FROM earnings_material em
            WHERE UPPER(TRIM(em.symbol)) = UPPER(TRIM(kb.ticker))
              AND em.event_date = kb.ts::date
          ) AS is_brand_new
        FROM knowledge_base kb
        WHERE {' AND '.join(where)}
        ORDER BY kb.ts DESC, kb.id DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


def pending_event_keys(events: list[dict[str, Any]]) -> set[tuple[str, date]]:
    out: set[tuple[str, date]] = set()
    for ev in events:
        sym = str(ev.get("symbol") or "").strip().upper()
        ev_date = ev.get("event_date")
        if sym and isinstance(ev_date, date):
            out.add((sym, ev_date))
    return out


def brand_new_event_keys(events: list[dict[str, Any]]) -> set[tuple[str, date]]:
    out: set[tuple[str, date]] = set()
    for ev in events:
        if not ev.get("is_brand_new"):
            continue
        sym = str(ev.get("symbol") or "").strip().upper()
        ev_date = ev.get("event_date")
        if sym and isinstance(ev_date, date):
            out.add((sym, ev_date))
    return out

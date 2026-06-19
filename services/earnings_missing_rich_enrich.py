"""Auto-enrich calendar events stuck on missing_rich_material (thin SEC only)."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.earnings_calendar_new_events import (
    RICH_MATERIAL_TYPES,
    load_materials_pipeline_calendar_events,
)
from services.earnings_event_date_match import resolve_kb_id_for_earnings_event
from services.earnings_material_auto_sources import auto_materials_for_event
from services.earnings_material_catalog import catalog_for_event

logger = logging.getLogger(__name__)

_RECLASSIFY_SQL = text(
    """
    UPDATE earnings_material em
    SET material_type = 'transcript',
        title = COALESCE(NULLIF(TRIM(title), ''), 'Earnings call transcript (auto-reclassified)'),
        updated_at = NOW()
    WHERE em.material_type = 'ir_event_page'
      AND em.parse_status IN ('parsed', 'extracted')
      AND LENGTH(COALESCE(em.content_text, '')) >= 5000
      AND (
        em.content_text ILIKE '%earnings call%'
        OR em.content_text ILIKE '%corrected transcript%'
      )
      AND NOT EXISTS (
        SELECT 1
        FROM earnings_material t
        WHERE UPPER(t.symbol) = UPPER(em.symbol)
          AND t.event_date = em.event_date
          AND t.material_type = 'transcript'
          AND t.source_url = em.source_url
      )
    RETURNING id, UPPER(symbol) AS symbol, event_date
    """
)

_UPSERT_SQL = text(
    """
    INSERT INTO earnings_material (
      knowledge_base_id, symbol, event_date, fiscal_period,
      material_type, source_name, source_url, title, meta, parse_status
    ) VALUES (
      :knowledge_base_id, :symbol, :event_date, :fiscal_period,
      :material_type, :source_name, :source_url, :title,
      CAST(:meta AS jsonb), 'registered'
    )
    ON CONFLICT (symbol, COALESCE(event_date, DATE '1900-01-01'), material_type, source_url)
    DO UPDATE SET
      knowledge_base_id = COALESCE(EXCLUDED.knowledge_base_id, earnings_material.knowledge_base_id),
      fiscal_period = COALESCE(EXCLUDED.fiscal_period, earnings_material.fiscal_period),
      title = COALESCE(EXCLUDED.title, earnings_material.title),
      meta = earnings_material.meta || EXCLUDED.meta,
      updated_at = NOW()
    RETURNING id
    """
)


def missing_rich_calendar_events(
    engine: Engine,
    *,
    since: date | None = None,
    symbols: set[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    events = load_materials_pipeline_calendar_events(
        engine,
        since=since,
        symbols=symbols,
        limit=limit,
        past_only=True,
    )
    return [
        ev
        for ev in events
        if "missing_rich_material" in str(ev.get("pipeline_reason") or "")
    ]


def reclassify_ir_event_transcripts(engine: Engine) -> list[tuple[int, str, date]]:
    with engine.begin() as conn:
        rows = conn.execute(_RECLASSIFY_SQL).fetchall()
    out = [(int(r[0]), str(r[1]), r[2]) for r in rows]
    for mid, sym, ev_d in out:
        logger.info("reclassified ir_event_page→transcript id=%s %s %s", mid, sym, ev_d)
    return out


def _upsert_catalog_material(engine: Engine, *, cm, kb_id: int | None) -> int | None:
    with engine.begin() as conn:
        row = conn.execute(
            _UPSERT_SQL,
            {
                "knowledge_base_id": kb_id,
                "symbol": cm.symbol.upper(),
                "event_date": cm.event_date,
                "fiscal_period": cm.fiscal_period,
                "material_type": cm.material_type,
                "source_name": cm.source_name,
                "source_url": cm.source_url,
                "title": cm.title,
                "meta": '{"auto_enrich":"missing_rich_material"}',
            },
        ).first()
    return int(row[0]) if row else None


def enrich_missing_rich_materials(
    engine: Engine,
    *,
    since: date | None = None,
    symbols: set[str] | None = None,
    limit: int = 100,
    include_fool: bool = True,
) -> dict[str, Any]:
    """
    Register catalog/auto rich sources for events with missing_rich_material,
    then reclassify obvious IR PDF transcripts.
    """
    events = missing_rich_calendar_events(engine, since=since, symbols=symbols, limit=limit)
    registered = 0
    enriched_keys: list[tuple[str, date]] = []
    seen_urls: set[str] = set()

    for ev in events:
        sym = str(ev.get("symbol") or "").strip().upper()
        ev_date = ev.get("event_date")
        if not sym or not isinstance(ev_date, date):
            continue
        kb_id = resolve_kb_id_for_earnings_event(engine, symbol=sym, event_date=ev_date)
        candidates = list(catalog_for_event(sym, ev_date))
        candidates.extend(
            auto_materials_for_event(
                sym,
                ev_date,
                include_catalog=False,
                include_fool=include_fool,
            )
        )
        added_for_event = False
        for cm in candidates:
            if cm.material_type not in RICH_MATERIAL_TYPES:
                continue
            if cm.source_url in seen_urls:
                continue
            seen_urls.add(cm.source_url)
            mid = _upsert_catalog_material(engine, cm=cm, kb_id=kb_id)
            if mid:
                registered += 1
                added_for_event = True
                logger.info(
                    "enrich registered id=%s %s %s %s",
                    mid,
                    sym,
                    ev_date,
                    cm.material_type,
                )
        if added_for_event:
            enriched_keys.append((sym, ev_date))

    reclassified = reclassify_ir_event_transcripts(engine)
    return {
        "missing_rich_events": len(events),
        "registered": registered,
        "reclassified": len(reclassified),
        "enriched_keys": enriched_keys,
    }

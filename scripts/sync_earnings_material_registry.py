#!/usr/bin/env python3
"""
Sync earnings_material registry from knowledge_base EARNINGS calendar + catalog URLs.

Flow:
  1. Read KB EARNINGS events (yfinance / Alpha Vantage / manual).
  2. Attach catalog materials for matching (symbol, event_date).
  3. Optionally expand discovered_links from already parsed ir_event_page rows.
  4. Upsert idempotent rows into earnings_material.

Examples:
  python scripts/sync_earnings_material_registry.py --dry-run
  python scripts/sync_earnings_material_registry.py --ensure-table --since 2026-01-01
  python scripts/sync_earnings_material_registry.py --symbols META,NVDA --discover-links
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_material_catalog import (  # noqa: E402
    CatalogMaterial,
    catalog_for_event,
    priority_catalog,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DISCOVERED_LINK_TYPES = (
    "press_release",
    "presentation",
    "transcript",
    "follow_up_transcript",
    "sec_filing",
    "third_party_transcript",
    "other",
)


@dataclass(frozen=True)
class SyncRow:
    knowledge_base_id: int | None
    symbol: str
    event_date: date | None
    fiscal_period: str | None
    material_type: str
    source_name: str
    source_url: str
    title: str
    meta: dict


def _guess_material_type(url: str, title: str = "") -> str:
    u = (url or "").lower()
    t = (title or "").lower()
    blob = f"{u} {t}"
    if "follow-up" in blob or "followup" in blob or "follow_up" in blob:
        return "follow_up_transcript"
    if "transcript" in blob and ("fool.com" in u or "seekingalpha" in u):
        return "third_party_transcript"
    if "transcript" in blob:
        return "transcript"
    if "presentation" in blob or "slides" in blob:
        return "presentation"
    if "press-release" in blob or "press_release" in blob or "prnewswire" in u or "news-releases" in u:
        return "press_release"
    if u.endswith(".pdf"):
        if "transcript" in blob:
            return "transcript"
        if "presentation" in blob or "slide" in blob:
            return "presentation"
        return "other"
    if "sec.gov" in u:
        return "sec_filing"
    if "investor" in u or "financial-results" in u or "event-details" in u:
        return "ir_event_page"
    return "other"


def _ensure_table(engine) -> None:
    sql_path = project_root / "scripts" / "sql" / "ml_event_analytics_schema.sql"
    raw = sql_path.read_text(encoding="utf-8")
    buf: list[str] = []
    with engine.begin() as conn:
        for line in raw.splitlines():
            if line.strip().startswith("--"):
                continue
            buf.append(line)
            if line.strip().endswith(";"):
                stmt = "\n".join(buf).strip()
                buf = []
                if stmt:
                    conn.execute(text(stmt))


def _load_kb_events(
    engine,
    *,
    since: date | None,
    until: date | None,
    symbols: set[str] | None,
    limit: int,
) -> list[dict]:
    where = ["UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'"]
    params: dict = {"limit": int(limit)}
    if since:
        where.append("ts::date >= :since")
        params["since"] = since
    if until:
        where.append("ts::date <= :until")
        params["until"] = until
    if symbols:
        where.append("UPPER(TRIM(ticker)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    q = text(
        f"""
        SELECT id, UPPER(TRIM(ticker)) AS symbol, ts::date AS event_date, source, content
        FROM knowledge_base
        WHERE {' AND '.join(where)}
        ORDER BY ts DESC, id DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


def _load_discovered_links(engine) -> list[dict]:
    q = text(
        """
        SELECT id, symbol, event_date, fiscal_period, knowledge_base_id, source_url, meta
        FROM earnings_material
        WHERE parse_status IN ('parsed', 'downloaded', 'registered')
          AND meta ? 'discovered_links'
        ORDER BY updated_at DESC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    out: list[dict] = []
    for r in rows:
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        for link in meta.get("discovered_links") or []:
            out.append(
                {
                    "parent_material_id": r["id"],
                    "symbol": r["symbol"],
                    "event_date": r.get("event_date"),
                    "fiscal_period": r.get("fiscal_period"),
                    "knowledge_base_id": r.get("knowledge_base_id"),
                    "source_url": link,
                }
            )
    return out


def _catalog_row(cm: CatalogMaterial, *, kb_id: int | None, sync_source: str) -> SyncRow:
    meta = dict(cm.meta or {})
    meta["sync_source"] = sync_source
    return SyncRow(
        knowledge_base_id=kb_id,
        symbol=cm.symbol.upper(),
        event_date=cm.event_date,
        fiscal_period=cm.fiscal_period,
        material_type=cm.material_type,
        source_name=cm.source_name,
        source_url=cm.source_url,
        title=cm.title,
        meta=meta,
    )


def _discovered_row(item: dict) -> SyncRow | None:
    url = str(item.get("source_url") or "").strip()
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    title = url.rsplit("/", 1)[-1].replace("-", " ")
    mtype = _guess_material_type(url, title)
    return SyncRow(
        knowledge_base_id=item.get("knowledge_base_id"),
        symbol=str(item["symbol"]).upper(),
        event_date=item.get("event_date"),
        fiscal_period=item.get("fiscal_period"),
        material_type=mtype,
        source_name=host or "discovered",
        source_url=url,
        title=title[:200],
        meta={
            "sync_source": "discovered_links",
            "parent_material_id": item.get("parent_material_id"),
        },
    )


def build_sync_rows(
    engine,
    *,
    since: date | None,
    until: date | None,
    symbols: set[str] | None,
    limit: int,
    include_priority_catalog: bool,
    discover_links: bool,
) -> list[SyncRow]:
    rows: list[SyncRow] = []
    seen: set[tuple[str, str | None, str, str]] = set()

    def add(row: SyncRow) -> None:
        key = (
            row.symbol,
            str(row.event_date) if row.event_date else None,
            row.material_type,
            row.source_url,
        )
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    kb_events = _load_kb_events(engine, since=since, until=until, symbols=symbols, limit=limit)
    logger.info("KB earnings events loaded: %s", len(kb_events))
    for ev in kb_events:
        kb_id = int(ev["id"])
        sym = str(ev["symbol"]).upper()
        ev_date = ev.get("event_date")
        if not isinstance(ev_date, date):
            continue
        for cm in catalog_for_event(sym, ev_date):
            add(_catalog_row(cm, kb_id=kb_id, sync_source="kb+catalog"))

    if include_priority_catalog:
        for cm in priority_catalog():
            if symbols and cm.symbol not in symbols:
                continue
            add(_catalog_row(cm, kb_id=None, sync_source="priority_catalog"))

    if discover_links:
        for item in _load_discovered_links(engine):
            if symbols and str(item["symbol"]).upper() not in symbols:
                continue
            row = _discovered_row(item)
            if row:
                add(row)

    return rows


def upsert_rows(engine, rows: Iterable[SyncRow], *, dry_run: bool) -> int:
    upsert = text(
        """
        INSERT INTO earnings_material (
          knowledge_base_id, symbol, event_date, fiscal_period,
          material_type, source_name, source_url, title, meta, updated_at
        )
        VALUES (
          :knowledge_base_id, :symbol, :event_date, :fiscal_period,
          :material_type, :source_name, :source_url, :title,
          CAST(:meta AS jsonb), NOW()
        )
        ON CONFLICT (
          symbol,
          (COALESCE(event_date, DATE '1900-01-01')),
          material_type,
          source_url
        ) DO UPDATE SET
          knowledge_base_id = COALESCE(EXCLUDED.knowledge_base_id, earnings_material.knowledge_base_id),
          fiscal_period = COALESCE(EXCLUDED.fiscal_period, earnings_material.fiscal_period),
          source_name = EXCLUDED.source_name,
          title = EXCLUDED.title,
          meta = earnings_material.meta || EXCLUDED.meta,
          updated_at = NOW()
        """
    )
    n = 0
    with engine.begin() as conn:
        for row in rows:
            params = {
                "knowledge_base_id": row.knowledge_base_id,
                "symbol": row.symbol,
                "event_date": row.event_date,
                "fiscal_period": row.fiscal_period,
                "material_type": row.material_type,
                "source_name": row.source_name,
                "source_url": row.source_url,
                "title": row.title,
                "meta": json.dumps(row.meta, ensure_ascii=False),
            }
            if dry_run:
                logger.info("dry-run sync: %s", params)
            else:
                conn.execute(upsert, params)
            n += 1
    return n


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync earnings_material from KB calendar + catalog")
    ap.add_argument("--ensure-table", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--until", default="")
    ap.add_argument("--symbols", default="", help="Comma-separated; default all from KB/catalog")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--priority-catalog", action="store_true", default=True)
    ap.add_argument("--no-priority-catalog", action="store_false", dest="priority_catalog")
    ap.add_argument("--discover-links", action="store_true", help="Add discovered_links from parsed IR pages")
    args = ap.parse_args()

    symbols = None
    if args.symbols.strip():
        symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    engine = get_engine()
    if args.ensure_table:
        _ensure_table(engine)

    rows = build_sync_rows(
        engine,
        since=_parse_date(args.since),
        until=_parse_date(args.until),
        symbols=symbols,
        limit=max(1, args.limit),
        include_priority_catalog=args.priority_catalog,
        discover_links=args.discover_links,
    )
    n = upsert_rows(engine, rows, dry_run=args.dry_run)
    logger.info("%s %s earnings_material sync rows", "Checked" if args.dry_run else "Upserted", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

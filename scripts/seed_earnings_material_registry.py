#!/usr/bin/env python3
"""
Seed the earnings_material registry with starter IR/report source URLs.

Deprecated for ongoing ops: prefer scripts/sync_earnings_material_registry.py.
This script now loads the shared priority catalog.

Examples:
  python scripts/seed_earnings_material_registry.py --ensure-table --dry-run
  python scripts/seed_earnings_material_registry.py --ensure-table
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_material_catalog import CatalogMaterial, priority_catalog  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

STARTER_MATERIALS: tuple[CatalogMaterial, ...] = priority_catalog()


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


def _find_kb_id(conn, seed: CatalogMaterial) -> int | None:
    if seed.event_date is None:
        return None
    row = conn.execute(
        text(
            """
            SELECT id
            FROM knowledge_base
            WHERE UPPER(TRIM(ticker)) = :symbol
              AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
              AND ts::date = :event_date
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"symbol": seed.symbol.upper(), "event_date": seed.event_date},
    ).fetchone()
    return int(row[0]) if row else None


def _upsert_materials(engine, seeds: Iterable[CatalogMaterial], *, dry_run: bool) -> int:
    if dry_run:
        n = 0
        for seed in seeds:
            params = {
                "knowledge_base_id": None,
                "symbol": seed.symbol.upper(),
                "event_date": seed.event_date,
                "fiscal_period": seed.fiscal_period,
                "material_type": seed.material_type,
                "source_name": seed.source_name,
                "source_url": seed.source_url,
                "title": seed.title,
                "meta": seed.meta or {},
            }
            logger.info("dry-run registry row: %s", params)
            n += 1
        return n

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
        for seed in seeds:
            kb_id = _find_kb_id(conn, seed)
            params = {
                "knowledge_base_id": kb_id,
                "symbol": seed.symbol.upper(),
                "event_date": seed.event_date,
                "fiscal_period": seed.fiscal_period,
                "material_type": seed.material_type,
                "source_name": seed.source_name,
                "source_url": seed.source_url,
                "title": seed.title,
                "meta": json.dumps(seed.meta or {}, ensure_ascii=False),
            }
            conn.execute(upsert, params)
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed starter earnings_material registry rows")
    ap.add_argument("--ensure-table", action="store_true", help="Apply scripts/sql/ml_event_analytics_schema.sql first")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = get_engine() if (args.ensure_table or not args.dry_run) else None
    if args.ensure_table:
        assert engine is not None
        _ensure_table(engine)
    assert engine is not None or args.dry_run
    n = _upsert_materials(engine, STARTER_MATERIALS, dry_run=args.dry_run)
    logger.info("%s %s earnings_material registry rows", "Checked" if args.dry_run else "Upserted", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

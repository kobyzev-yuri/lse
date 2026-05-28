#!/usr/bin/env python3
"""
Seed the earnings_material registry with starter IR/report source URLs.

This does not download or parse remote pages. It only creates idempotent registry
rows so the next downloader/parser step has explicit work items.

Examples:
  python scripts/seed_earnings_material_registry.py --ensure-table --dry-run
  python scripts/seed_earnings_material_registry.py --ensure-table
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaterialSeed:
    symbol: str
    material_type: str
    source_url: str
    source_name: str
    title: str
    event_date: date | None = None
    fiscal_period: str | None = None
    meta: dict | None = None


STARTER_MATERIALS: tuple[MaterialSeed, ...] = (
    MaterialSeed(
        symbol="META",
        event_date=date(2026, 4, 29),
        fiscal_period="Q1 2026",
        material_type="ir_event_page",
        source_name="Meta Investor Relations",
        source_url="https://investor.atmeta.com/investor-events/event-details/2026/Q1-2026-Earnings-Call/default.aspx",
        title="Meta Q1 2026 earnings call event page",
        meta={"mvp_case": "META capex -> AI infrastructure peers"},
    ),
    MaterialSeed(
        symbol="ASML",
        event_date=date(2026, 4, 15),
        fiscal_period="Q1 2026",
        material_type="ir_event_page",
        source_name="ASML Investor Relations",
        source_url="https://www.asml.com/en/investors/financial-results/q1-2026",
        title="ASML Q1 2026 financial results",
        meta={"mvp_case": "ASML pullback/rebound reference case"},
    ),
    MaterialSeed(
        symbol="ARM",
        event_date=date(2026, 5, 6),
        fiscal_period=None,
        material_type="ir_event_page",
        source_name="Arm Investor Relations",
        source_url="https://investors.arm.com/financials/quarterly-annual-results",
        title="Arm quarterly and annual results page",
        meta={"mvp_case": "ARM cross-earnings into AI/chip peers"},
    ),
    MaterialSeed(
        symbol="SNDK",
        event_date=date(2026, 4, 30),
        fiscal_period="Q3 2026",
        material_type="third_party_transcript",
        source_name="The Motley Fool",
        source_url="https://www.fool.com/earnings/call-transcripts/2026/04/30/sandisk-sndk-q3-2026-earnings-transcript/",
        title="SanDisk Q3 2026 earnings transcript",
        meta={"mvp_case": "SNDK demand-dominant follow-through reference case"},
    ),
    MaterialSeed(
        symbol="NVDA",
        event_date=date(2026, 5, 20),
        fiscal_period=None,
        material_type="ir_event_page",
        source_name="NVIDIA Investor Relations",
        source_url="https://investor.nvidia.com/",
        title="NVIDIA investor relations financial results hub",
        meta={"mvp_case": "NVDA earnings -> AI basket"},
    ),
)


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


def _find_kb_id(conn, seed: MaterialSeed) -> int | None:
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


def _upsert_materials(engine, seeds: Iterable[MaterialSeed], *, dry_run: bool) -> int:
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

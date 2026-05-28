#!/usr/bin/env python3
"""
Seed peer_graph_edge from services/peer_graph_catalog.py (idempotent upsert).

Examples:
  python scripts/seed_peer_graph_edges.py --dry-run
  python scripts/seed_peer_graph_edges.py --source META,NVDA
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.peer_graph_catalog import PEER_GRAPH_EDGES, PeerGraphEdge  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


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


def _upsert_edges(engine, edges: list[PeerGraphEdge], *, dry_run: bool) -> int:
    upsert = text(
        """
        INSERT INTO peer_graph_edge (
          source_ticker, target_ticker, relation_type, weight, valid_from, meta
        )
        VALUES (
          :source_ticker, :target_ticker, :relation_type, :weight, :valid_from,
          CAST(:meta AS jsonb)
        )
        ON CONFLICT (source_ticker, target_ticker, relation_type, valid_from)
        DO UPDATE SET
          weight = EXCLUDED.weight,
          meta = COALESCE(peer_graph_edge.meta, '{}'::jsonb) || EXCLUDED.meta
        """
    )
    n = 0
    with engine.begin() as conn:
        for edge in edges:
            params = {
                "source_ticker": edge.source_ticker.upper(),
                "target_ticker": edge.target_ticker.upper(),
                "relation_type": edge.relation_type,
                "weight": edge.weight,
                "valid_from": edge.valid_from,
                "meta": json.dumps(edge.meta or {}, ensure_ascii=False),
            }
            if dry_run:
                logger.info("dry-run edge: %s", params)
            else:
                conn.execute(upsert, params)
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed peer_graph_edge MVP catalog")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ensure-table", action="store_true")
    ap.add_argument("--source", default="", help="Comma-separated source tickers filter")
    args = ap.parse_args()

    sources = {s.strip().upper() for s in args.source.split(",") if s.strip()} or None
    edges = list(PEER_GRAPH_EDGES)
    if sources:
        edges = [e for e in edges if e.source_ticker in sources]

    engine = get_engine()
    if args.ensure_table:
        _ensure_table(engine)

    n = _upsert_edges(engine, edges, dry_run=args.dry_run)
    logger.info("%s %s peer_graph_edge rows", "Would upsert" if args.dry_run else "Upserted", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

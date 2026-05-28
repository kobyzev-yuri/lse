#!/usr/bin/env python3
"""
Миграция: таблицы event / earnings analytics и event_reaction_dataset.

SQL-источник: scripts/sql/ml_event_analytics_schema.sql

  python scripts/migrate_ml_event_analytics.py

На проде (Docker): после git pull пересобрать образ или выполнить SQL через psql.
См. docs/DATABASE_SCHEMA.md, docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text

from config_loader import get_database_url


def main() -> int:
    sql_path = project_root / "scripts" / "sql" / "ml_event_analytics_schema.sql"
    if not sql_path.is_file():
        print(f"❌ Нет файла {sql_path}")
        return 1
    raw = sql_path.read_text(encoding="utf-8")
    chunks: list[str] = []
    buf: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                chunks.append(stmt.rstrip(";").strip())
            buf = []
    if buf:
        stmt = "\n".join(buf).strip()
        if stmt:
            chunks.append(stmt.rstrip(";").strip())

    engine = create_engine(get_database_url())
    with engine.begin() as conn:
        for stmt in chunks:
            if not stmt:
                continue
            conn.execute(text(stmt + ";"))
    print("✅ ml_event_analytics_schema: OK (earnings_event_detail, earnings_material, peer_graph_edge, market_regime_daily, event_reaction_dataset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

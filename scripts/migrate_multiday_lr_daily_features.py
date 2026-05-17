#!/usr/bin/env python3
"""
Apply DDL for multiday ridge daily feature tables (news, macro calendar, symbol calendar).

  python scripts/migrate_multiday_lr_daily_features.py

On VM:
  docker exec lse-bot python scripts/migrate_multiday_lr_daily_features.py
"""

from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

SQL_FILES = (
    "023_news_daily_features.sql",
    "024_macro_calendar_daily_features.sql",
    "025_symbol_calendar_daily_features.sql",
)


def _sql_statements(ddl: str) -> list[str]:
    """Split DDL on statement boundaries (semicolon + newline), not on ';' inside strings."""
    import re

    lines: list[str] = []
    for line in ddl.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    parts = re.split(r";\s*\n", body)
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if part:
            out.append(part + ";")
    return out


def main() -> int:
    from sqlalchemy import create_engine, text

    from config_loader import get_database_url

    engine = create_engine(get_database_url())
    sql_dir = project_root / "db" / "knowledge_pg" / "sql"
    for name in SQL_FILES:
        path = sql_dir / name
        if not path.is_file():
            print(f"MISSING {path}")
            return 1
        ddl = path.read_text(encoding="utf-8")
        with engine.begin() as conn:
            for stmt in _sql_statements(ddl):
                conn.execute(text(stmt))
        print(f"OK {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

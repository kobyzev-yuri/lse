#!/usr/bin/env python3
"""
Создаёт таблицы market_bars_5m и market_bars_30m (если ещё нет).

  python scripts/migrate_market_bars_intraday.py

SQL-источник: db/knowledge_pg/sql/021_market_bars_5m_30m.sql
На VM: docker compose exec lse python scripts/migrate_market_bars_intraday.py
"""

from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import Engine, create_engine, text

from config_loader import get_database_url


def apply_market_bars_intraday_schema(engine: Engine) -> None:
    sql_path = project_root / "db" / "knowledge_pg" / "sql" / "021_market_bars_5m_30m.sql"
    if not sql_path.is_file():
        raise FileNotFoundError(f"Нет файла: {sql_path}")
    ddl = sql_path.read_text(encoding="utf-8")
    parts = [p.strip() for p in ddl.split(";")]
    with engine.begin() as conn:
        for p in parts:
            if p:
                conn.execute(text(p + ";"))


def main() -> None:
    engine = create_engine(get_database_url())
    apply_market_bars_intraday_schema(engine)
    print("OK: market_bars_5m, market_bars_30m")


if __name__ == "__main__":
    main()

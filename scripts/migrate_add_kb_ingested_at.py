#!/usr/bin/env python3
"""
Разовая миграция: колонка knowledge_base.ingested_at (время загрузки в KB).

Без неё падают запросы с ORDER BY COALESCE(ingested_at, ts).
Тот же SQL: scripts/sql/add_knowledge_base_ingested_at.sql

На VM образ lse-bot собирается из репозитория: после git pull новый скрипт
появляется только в ~/lse на диске, но не внутри старого контейнера, пока не
сделать docker compose build lse (или ./scripts/deploy_from_github.sh).

Варианты:
  A) Без пересборки — SQL напрямую в Postgres:
     docker exec lse-postgres psql -U postgres -d lse_trading -c "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ;"
  B) После пересборки образа:
     docker exec lse-bot python scripts/migrate_add_kb_ingested_at.py
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text

from config_loader import get_database_url


def main() -> None:
    engine = create_engine(get_database_url())
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ"
            )
        )
    print("✅ knowledge_base.ingested_at: OK")


if __name__ == "__main__":
    main()

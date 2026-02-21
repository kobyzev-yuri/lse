#!/usr/bin/env python3
"""
Одноразовая миграция: перенос данных из trade_kb в knowledge_base.
После миграции embedding и outcome_json хранятся в knowledge_base; таблица trade_kb удаляется.

Запуск: python scripts/migrate_trade_kb_to_knowledge_base.py
  При необходимости скрипт сам добавит в knowledge_base колонки embedding и outcome_json.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from sqlalchemy import create_engine, text

from config_loader import get_database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def table_exists(conn, name: str) -> bool:
    r = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :n"),
        {"n": name},
    )
    return r.fetchone() is not None


def run():
    db_url = get_database_url()
    engine = create_engine(db_url)

    with engine.connect() as conn:
        if not table_exists(conn, "trade_kb"):
            logger.info("Таблица trade_kb отсутствует — миграция не требуется")
            return
        if not table_exists(conn, "knowledge_base"):
            logger.error("Таблица knowledge_base не найдена. Сначала выполните init_db.")
            sys.exit(1)

        # Проверяем наличие колонок embedding, outcome_json в knowledge_base
        r = conn.execute(
            text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'knowledge_base' AND column_name IN ('embedding', 'outcome_json')
            """)
        )
        cols = {row[0] for row in r}
        has_kb_id = conn.execute(
            text("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'trade_kb' AND column_name = 'knowledge_base_id'
            """)
        ).fetchone() is not None

    # Добавляем колонки в knowledge_base, если их ещё нет
    if "embedding" not in cols or "outcome_json" not in cols:
        logger.info("Добавляем колонки embedding и outcome_json в knowledge_base...")
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            if "embedding" not in cols:
                conn.execute(text("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding vector(768)"))
            if "outcome_json" not in cols:
                conn.execute(text("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS outcome_json JSONB"))
        logger.info("Колонки добавлены.")

    logger.info("Копирование данных из trade_kb в knowledge_base...")

    with engine.begin() as conn:
        updated = 0
        if has_kb_id:
            r = conn.execute(
                text("""
                    UPDATE knowledge_base kb
                    SET embedding = tk.embedding, outcome_json = tk.outcome_json
                    FROM trade_kb tk
                    WHERE tk.knowledge_base_id = kb.id AND tk.knowledge_base_id IS NOT NULL
                """)
            )
            updated = r.rowcount
            logger.info("Обновлено записей в knowledge_base (из trade_kb по knowledge_base_id): %s", updated)

        # Строки без knowledge_base_id (или если колонки не было): вставляем как новые записи
        where = "WHERE knowledge_base_id IS NULL" if has_kb_id else ""
        r = conn.execute(
            text(f"""
                INSERT INTO knowledge_base (ts, ticker, source, content, event_type, embedding, outcome_json)
                SELECT ts, ticker, 'MANUAL', content, COALESCE(event_type, 'NEWS'), embedding, outcome_json
                FROM trade_kb
                {where}
            """)
        )
        inserted = r.rowcount
        logger.info("Вставлено новых записей в knowledge_base (из trade_kb без kb_id): %s", inserted)

        conn.execute(text("DROP TABLE IF EXISTS trade_kb"))
        logger.info("Таблица trade_kb удалена.")

    logger.info("Миграция завершена. Дальше используйте только knowledge_base.")


if __name__ == "__main__":
    run()

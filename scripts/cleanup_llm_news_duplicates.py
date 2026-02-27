#!/usr/bin/env python3
"""
Удаление дубликатов LLM-новостей в knowledge_base.

Причина дублей: cron fetch_news_cron вызывал fetch_and_save_llm_news каждый раз
(например каждый час). LLM возвращает одни и те же факты (без реального времени),
поэтому накапливались десятки записей с одинаковым содержанием и разными ts.

Скрипт оставляет по одной LLM-записи на тикер на календарный день (последнюю по ts),
остальные удаляет.

Использование:
  python scripts/cleanup_llm_news_duplicates.py           # dry-run: только показать, что удалится
  python scripts/cleanup_llm_news_duplicates.py --apply  # выполнить удаление
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


def cleanup_llm_duplicates(apply: bool = False) -> int:
    """
    Удаляет дубликаты LLM-новостей: оставляет одну запись на (ticker, день), остальные удаляет.
    Возвращает количество удалённых строк.
    """
    engine = create_engine(get_database_url())
    deleted = 0

    with engine.connect() as conn:
        # Список id записей, которые нужно удалить: LLM-источник, не последняя в своей группе (ticker, date)
        to_delete = conn.execute(
            text("""
                WITH llm_news AS (
                    SELECT id, ticker, ts::date AS day,
                           ROW_NUMBER() OVER (PARTITION BY ticker, ts::date ORDER BY ts DESC, id DESC) AS rn
                    FROM knowledge_base
                    WHERE source LIKE 'LLM%'
                )
                SELECT id FROM llm_news WHERE rn > 1
            """)
        ).fetchall()
        ids_to_delete = [r[0] for r in to_delete]

        if not ids_to_delete:
            logger.info("Дубликатов LLM-новостей не найдено.")
            return 0

        logger.info("Найдено дубликатов LLM-новостей: %s (оставляем по 1 на тикер/день)", len(ids_to_delete))
        if apply:
            for id_ in ids_to_delete:
                conn.execute(text("DELETE FROM knowledge_base WHERE id = :id"), {"id": id_})
                deleted += 1
            conn.commit()
            logger.info("Удалено записей: %s", deleted)
        else:
            logger.info("Dry-run: не удаляю. Запустите с --apply для удаления.")

    return deleted


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    cleanup_llm_duplicates(apply=apply)

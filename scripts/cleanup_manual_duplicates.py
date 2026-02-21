#!/usr/bin/env python3
"""
Удаление дубликатов MANUAL в knowledge_base.

Удаляет записи с source='MANUAL', для которых есть другая запись (другой источник)
с тем же (ts, ticker, content). Опционально удаляет повторы среди MANUAL по (ts, ticker, content).

Запуск:
  python scripts/cleanup_manual_duplicates.py --dry-run   # только подсчёт
  python scripts/cleanup_manual_duplicates.py --execute  # выполнить удаление
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import argparse
import logging
from sqlalchemy import create_engine, text
from config_loader import get_database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def run(dry_run: bool = True, dedupe_manual_only: bool = True):
    """
    Находит и при --execute удаляет дубликаты MANUAL.

    Args:
        dry_run: если True, только подсчёт и отчёт, без DELETE
        dedupe_manual_only: если True, также удалять дубли среди MANUAL (оставлять одну запись на (ts, ticker, content))
    """
    db_url = get_database_url()
    engine = create_engine(db_url)

    with engine.connect() as conn:
        # 1) Сколько MANUAL дублируют запись с другим источником (тот же ts, ticker, content)
        r = conn.execute(
            text("""
                SELECT COUNT(*) FROM knowledge_base k
                WHERE k.source = 'MANUAL'
                  AND EXISTS (
                    SELECT 1 FROM knowledge_base o
                    WHERE o.source <> 'MANUAL'
                      AND o.ts = k.ts AND o.ticker = k.ticker AND o.content = k.content
                  )
            """)
        )
        dup_vs_other = r.scalar()

        # 2) Среди оставшихся MANUAL — дубли по (ts, ticker, content) (оставляем одну)
        r = conn.execute(
            text("""
                WITH manual_dupes AS (
                    SELECT id,
                           ROW_NUMBER() OVER (PARTITION BY ts, ticker, content ORDER BY id) AS rn
                    FROM knowledge_base
                    WHERE source = 'MANUAL'
                )
                SELECT COUNT(*) FROM manual_dupes WHERE rn > 1
            """)
        )
        dup_manual_only = r.scalar()

    logger.info(
        "MANUAL-дубликаты: %s штук совпадают с записью другого источника (ts, ticker, content); %s — лишние повторы среди MANUAL.",
        dup_vs_other,
        dup_manual_only,
    )

    if dry_run:
        logger.info("Режим --dry-run: удаление не выполнялось. Запустите с --execute для удаления.")
        return

    with engine.begin() as conn:
        # Удаляем MANUAL, у которых есть такая же запись с другим источником
        r = conn.execute(
            text("""
                DELETE FROM knowledge_base k
                WHERE k.source = 'MANUAL'
                  AND EXISTS (
                    SELECT 1 FROM knowledge_base o
                    WHERE o.source <> 'MANUAL'
                      AND o.ts = k.ts AND o.ticker = k.ticker AND o.content = k.content
                  )
            """)
        )
        deleted_vs_other = r.rowcount
        logger.info("Удалено MANUAL-дубликатов (есть запись с другим источником): %s", deleted_vs_other)

        if dedupe_manual_only and dup_manual_only > 0:
            # Удаляем лишние MANUAL с одинаковым (ts, ticker, content), оставляем строку с минимальным id
            r = conn.execute(
                text("""
                    DELETE FROM knowledge_base
                    WHERE id IN (
                        WITH ranked AS (
                            SELECT id, ROW_NUMBER() OVER (PARTITION BY ts, ticker, content ORDER BY id) AS rn
                            FROM knowledge_base
                            WHERE source = 'MANUAL'
                        )
                        SELECT id FROM ranked WHERE rn > 1
                    )
                """)
            )
            deleted_manual_only = r.rowcount
            logger.info("Удалено лишних MANUAL (повторы по ts, ticker, content): %s", deleted_manual_only)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Удаление дубликатов MANUAL в knowledge_base (по ts, ticker, content)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Только показать количество дубликатов (по умолчанию)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Выполнить удаление дубликатов",
    )
    parser.add_argument(
        "--no-dedupe-manual-only",
        action="store_true",
        help="Не удалять повторы среди только MANUAL (по умолчанию удаляем)",
    )
    args = parser.parse_args()
    dry_run = not args.execute
    dedupe_manual_only = not args.no_dedupe_manual_only
    run(dry_run=dry_run, dedupe_manual_only=dedupe_manual_only)

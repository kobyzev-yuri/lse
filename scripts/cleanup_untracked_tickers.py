#!/usr/bin/env python3
"""
Удаляет из knowledge_base все записи по тикерам, которые мы не отслеживаем.

«Наши» тикеры берутся из get_tracked_tickers_for_kb() (TICKERS_FAST, TICKERS_MEDIUM,
TICKERS_LONG + MACRO, US_MACRO). Всё остальное (CNCK, FLNG, DELTF, …) удаляется.

Запуск:
  python scripts/cleanup_untracked_tickers.py           # dry-run
  python scripts/cleanup_untracked_tickers.py --execute # удалить
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


def cleanup_untracked_tickers(dry_run: bool = True):
    from services.ticker_groups import get_tracked_tickers_for_kb

    tracked = get_tracked_tickers_for_kb()
    logger.info(f"📋 Отслеживаемые тикеры для KB: {sorted(tracked)}")

    db_url = get_database_url()
    engine = create_engine(db_url)

    with engine.connect() as conn:
        # Считаем, сколько записей с тикерами не из списка
        placeholders = ", ".join([f":t{i}" for i in range(len(tracked))])
        params = {f"t{i}": t for i, t in enumerate(tracked)}
        result = conn.execute(
            text(f"""
                SELECT ticker, COUNT(*) AS cnt
                FROM knowledge_base
                WHERE ticker IS NOT NULL AND ticker NOT IN ({placeholders})
                GROUP BY ticker
                ORDER BY cnt DESC
            """),
            params,
        )
        rows = result.fetchall()
        total_untracked = sum(r[1] for r in rows)

        if not rows:
            logger.info("✅ Записей по ненаблюдаемым тикерам нет.")
            engine.dispose()
            return

        logger.info(f"🗑️  Записей по ненаблюдаемым тикерам: {total_untracked}")
        for ticker, cnt in rows[:20]:
            logger.info(f"   {ticker}: {cnt}")
        if len(rows) > 20:
            logger.info(f"   ... и ещё {len(rows) - 20} тикеров")

        if dry_run:
            logger.info("\n⚠️  DRY RUN — ничего не удалено. Запустите с --execute для удаления.")
            engine.dispose()
            return

    with engine.begin() as conn:
        result = conn.execute(
            text(f"""
                DELETE FROM knowledge_base
                WHERE ticker IS NOT NULL AND ticker NOT IN ({placeholders})
            """),
            params,
        )
        deleted = result.rowcount
    logger.info(f"\n✅ Удалено записей: {deleted}")
    engine.dispose()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Удаление из KB записей по ненаблюдаемым тикерам")
    parser.add_argument("--execute", action="store_true", help="Реально удалить")
    args = parser.parse_args()
    cleanup_untracked_tickers(dry_run=not args.execute)

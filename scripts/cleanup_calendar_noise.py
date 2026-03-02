#!/usr/bin/env python3
"""
Скрипт для удаления мусорных записей календаря из knowledge_base.

1) ECONOMIC_INDICATOR с контентом в виде только числа (без текста).
2) Alpha Vantage Earnings Calendar: записи вида «Earnings report for TICKER» или
   «Earnings report for TICKER Estimate: X USD» — не несут пользы для решений, засоряют новости.

Рекомендуется запускать по cron раз в 1–7 дней, например:
  0 4 * * * cd /path/to/lse && python scripts/cleanup_calendar_noise.py --execute
"""

import re
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from sqlalchemy import create_engine, text
from config_loader import get_database_url

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Шаблон мусорной записи Earnings: "Earnings report for SYMBOL" или с одной строкой "Estimate: ..."
AV_EARNINGS_NOISE_PATTERN = re.compile(
    r"^Earnings report for [A-Z0-9\.\-]+(\s+Estimate:.*)?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def is_av_earnings_noise(content: str) -> bool:
    """True, если content — типичная бесполезная запись Alpha Vantage Earnings (только шаблон)."""
    if not content or not content.strip():
        return True
    text = content.strip()
    if len(text) > 200:
        return False
    return bool(AV_EARNINGS_NOISE_PATTERN.match(text))


def is_noise_content(content: str, event_name: str = None) -> bool:
    """
    Определяет, является ли запись шумом (только число без текста).
    """
    if not content or not content.strip():
        return True
    text = content.strip()
    # Короткий контент без пробелов (только число типа 19.60M)
    if len(text) < 25:
        return True
    if " " not in text:
        return True
    # Если есть название события, но оно слишком короткое
    if event_name and len(event_name.strip()) < 3:
        return True
    return False


def cleanup_calendar_noise(dry_run: bool = True):
    """
    Удаляет мусорные записи календаря из knowledge_base:
    - ECONOMIC_INDICATOR с контентом «только число»;
    - Alpha Vantage Earnings Calendar вида «Earnings report for TICKER» (без пользы).
    """
    db_url = get_database_url()
    engine = create_engine(db_url)
    total_deleted = 0

    with engine.connect() as conn:
        # 1) ECONOMIC_INDICATOR — только число без текста
        result = conn.execute(
            text("""
                SELECT id, source, content, event_type, ts
                FROM knowledge_base
                WHERE event_type = 'ECONOMIC_INDICATOR'
                ORDER BY ts DESC
            """)
        )
        rows = result.fetchall()
        logger.info(f"📊 Найдено {len(rows)} записей ECONOMIC_INDICATOR")

        to_delete = []
        for row in rows:
            content = row[2] or ''
            source = row[1] or ''
            event_name = ''
            if content:
                first_line = content.split('\n')[0].strip()
                if first_line and len(first_line) > 3:
                    event_name = first_line
            if is_noise_content(content, event_name):
                to_delete.append({
                    'id': row[0],
                    'source': source,
                    'content': content[:50] + '...' if len(content) > 50 else content,
                    'ts': row[4]
                })

        if to_delete:
            logger.info(f"🗑️  ECONOMIC_INDICATOR: {len(to_delete)} мусорных записей для удаления")
            for i, item in enumerate(to_delete[:3], 1):
                logger.info(f"  {i}. ID={item['id']}, {item['ts']}: {item['content']}")
            if len(to_delete) > 3:
                logger.info(f"  ... и еще {len(to_delete) - 3} записей")
            if not dry_run:
                with engine.begin() as trans_conn:
                    n = trans_conn.execute(
                        text("DELETE FROM knowledge_base WHERE id = ANY(:ids)"),
                        {"ids": [item['id'] for item in to_delete]}
                    ).rowcount
                total_deleted += n
                logger.info(f"  Удалено {n} записей ECONOMIC_INDICATOR")

        # 2) Alpha Vantage Earnings Calendar — «Earnings report for TICKER» (и с Estimate)
        #    Удаляем по source и дополнительно по content на случай другого source
        av_rows = conn.execute(
            text("""
                SELECT id, ticker, content, ts, source
                FROM knowledge_base
                WHERE source = 'Alpha Vantage Earnings Calendar'
                   OR content IS NOT NULL AND TRIM(content) LIKE 'Earnings report for %'
                ORDER BY ts DESC
            """)
        ).fetchall()

        av_to_delete = []
        seen_ids = set()
        for row in av_rows:
            if row[0] in seen_ids:
                continue
            content = row[2] or ''
            if is_av_earnings_noise(content):
                av_to_delete.append({
                    'id': row[0],
                    'ticker': row[1],
                    'content': (content[:60] + '...') if len(content) > 60 else content,
                    'ts': row[3],
                    'source': row[4] or ''
                })
                seen_ids.add(row[0])

        if av_to_delete:
            logger.info(f"🗑️  Alpha Vantage Earnings / «Earnings report for X»: {len(av_to_delete)} мусорных записей для удаления")
            for i, item in enumerate(av_to_delete[:5], 1):
                logger.info(f"  {i}. ID={item['id']}, {item['ts']} {item.get('ticker','')} [{item.get('source','')}]: {item['content']}")
            if len(av_to_delete) > 5:
                logger.info(f"  ... и еще {len(av_to_delete) - 5} записей")
            if not dry_run:
                with engine.begin() as trans_conn:
                    n = trans_conn.execute(
                        text("DELETE FROM knowledge_base WHERE id = ANY(:ids)"),
                        {"ids": [item['id'] for item in av_to_delete]}
                    ).rowcount
                total_deleted += n
                logger.info(f"  Удалено {n} записей Alpha Vantage Earnings / «Earnings report for X»")

    if dry_run and (to_delete or av_to_delete):
        logger.info("\n⚠️  Режим DRY RUN - записи не удалены. Запустите с --execute для удаления.")
    if total_deleted:
        logger.info(f"\n✅ Всего удалено {total_deleted} мусорных записей из knowledge_base")

    engine.dispose()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Очистка мусорных записей календаря из knowledge_base")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Реально удалить записи (по умолчанию только показывает что будет удалено)"
    )
    
    args = parser.parse_args()
    
    cleanup_calendar_noise(dry_run=not args.execute)

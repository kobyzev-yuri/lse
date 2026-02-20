#!/usr/bin/env python3
"""
Скрипт для удаления мусорных записей календаря из knowledge_base.
Удаляет записи ECONOMIC_INDICATOR с контентом в виде только числа (без текста).
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from sqlalchemy import create_engine, text
from config_loader import get_database_url

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


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
    Удаляет мусорные записи календаря из knowledge_base.
    
    Args:
        dry_run: Если True, только показывает что будет удалено, не удаляет
    """
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    with engine.connect() as conn:
        # Находим все ECONOMIC_INDICATOR записи
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
            # Извлекаем название события из content (первая строка)
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
        
        logger.info(f"🗑️  Найдено {len(to_delete)} мусорных записей для удаления")
        
        if to_delete:
            logger.info("\nПримеры записей для удаления:")
            for i, item in enumerate(to_delete[:5], 1):
                logger.info(f"  {i}. ID={item['id']}, {item['ts']}, {item['source']}: {item['content']}")
            if len(to_delete) > 5:
                logger.info(f"  ... и еще {len(to_delete) - 5} записей")
        
        if not dry_run and to_delete:
            with engine.begin() as trans_conn:
                deleted_count = trans_conn.execute(
                    text("""
                        DELETE FROM knowledge_base
                        WHERE id = ANY(:ids)
                    """),
                    {"ids": [item['id'] for item in to_delete]}
                ).rowcount
            logger.info(f"\n✅ Удалено {deleted_count} мусорных записей из knowledge_base")
        elif dry_run:
            logger.info("\n⚠️  Режим DRY RUN - записи не удалены")
            logger.info("   Запустите с --execute для реального удаления")
    
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

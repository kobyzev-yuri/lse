#!/usr/bin/env python3
"""
Cron скрипт для синхронизации knowledge_base → trade_kb
Генерирует embeddings для новых новостей и добавляет их в векторную БД
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
import os
from datetime import datetime

from services.vector_kb import VectorKB

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/sync_vector_kb.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def main():
    """Основная функция синхронизации"""
    logger.info("=" * 60)
    logger.info("🔄 Начало синхронизации Vector KB")
    logger.info("=" * 60)
    
    try:
        # Инициализация VectorKB
        vector_kb = VectorKB()
        
        # Получаем лимит из переменной окружения (если задан)
        limit = None
        if os.getenv('VECTOR_KB_SYNC_LIMIT'):
            try:
                limit = int(os.getenv('VECTOR_KB_SYNC_LIMIT'))
            except ValueError:
                logger.warning(f"⚠️ Неверное значение VECTOR_KB_SYNC_LIMIT: {os.getenv('VECTOR_KB_SYNC_LIMIT')}")
        
        # Размер батча из переменной окружения
        batch_size = 100
        if os.getenv('VECTOR_KB_BATCH_SIZE'):
            try:
                batch_size = int(os.getenv('VECTOR_KB_BATCH_SIZE'))
            except ValueError:
                logger.warning(f"⚠️ Неверное значение VECTOR_KB_BATCH_SIZE: {os.getenv('VECTOR_KB_BATCH_SIZE')}")
        
        # Синхронизация
        vector_kb.sync_from_knowledge_base(limit=limit, batch_size=batch_size)
        
        # Статистика
        stats = vector_kb.get_stats()
        logger.info("📊 Статистика trade_kb:")
        logger.info(f"   Всего событий: {stats.get('total_events', 0)}")
        logger.info(f"   С embeddings: {stats.get('with_embedding', 0)}")
        logger.info(f"   По типам: {stats.get('by_event_type', {})}")
        
        logger.info("=" * 60)
        logger.info("✅ Синхронизация Vector KB завершена")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка синхронизации: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

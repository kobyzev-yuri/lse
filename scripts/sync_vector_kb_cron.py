#!/usr/bin/env python3
"""
Cron скрипт: backfill embedding в knowledge_base.
Проставляет embedding для записей, у которых он ещё не заполнен (одна таблица для новостей и векторов).
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
Path(project_root / "logs").mkdir(parents=True, exist_ok=True)

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
        
        # Проверка: сколько записей без embedding (явно перед началом работы)
        without_emb = vector_kb.count_without_embedding()
        logger.info(f"📋 Проверка: записей без embedding (готовых к backfill): {without_emb}")
        if without_emb == 0:
            logger.info("   Нечего обрабатывать. Статистика ниже.")
        
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
        
        vector_kb.sync_from_knowledge_base(limit=limit, batch_size=batch_size)
        
        stats = vector_kb.get_stats()
        logger.info("📊 Статистика knowledge_base после запуска:")
        logger.info(f"   Всего событий: {stats.get('total_events', 0)}")
        logger.info(f"   С embeddings: {stats.get('with_embedding', 0)}")
        without = stats.get('without_embedding', 0) or 0
        skipped = stats.get('without_embedding_skipped_content', 0) or 0
        logger.info(f"   Без embeddings: {without}" + (f" (из них пропущено из-за пустого/короткого content: {skipped})" if skipped > 0 else ""))
        logger.info(f"   По типам (с embedding): {stats.get('by_event_type', {})}")
        
        logger.info("=" * 60)
        logger.info("✅ Синхронизация Vector KB завершена")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка синхронизации: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Cron скрипт для добавления sentiment анализа к новостям без sentiment
Использует LLM для расчета sentiment для новостей из RSS и NewsAPI
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
Path(project_root / "logs").mkdir(parents=True, exist_ok=True)

import logging
import os
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text
import pandas as pd

from config_loader import get_database_url, get_config_value
from services.sentiment_analyzer import calculate_sentiment

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/add_sentiment_to_news.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def add_sentiment_to_news(
    days_back: int = 1,
    limit: int = None,
    batch_size: int = 10,
    min_content_length: int = 20
):
    """
    Добавляет sentiment анализ к новостям без sentiment
    
    Args:
        days_back: Анализировать новости за последние N дней
        limit: Максимальное количество новостей для анализа (если None - все подходящие)
        batch_size: Размер батча для обработки (LLM запросы)
        min_content_length: Минимальная длина контента для анализа
    """
    logger.info("=" * 60)
    logger.info("🔄 Начало добавления sentiment анализа к новостям")
    logger.info("=" * 60)
    
    # Проверяем способ расчёта sentiment: transformers можно без USE_LLM
    sentiment_method = (get_config_value('SENTIMENT_METHOD', 'llm') or 'llm').strip().lower()
    if sentiment_method == 'llm':
        use_llm = get_config_value('USE_LLM', 'false').lower() == 'true'
        if not use_llm:
            logger.warning("⚠️ SENTIMENT_METHOD=llm и USE_LLM=false — sentiment через LLM недоступен")
            return
    else:
        logger.info("📊 Sentiment через бесплатную модель (SENTIMENT_METHOD=%s)", sentiment_method)
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    analyzed_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0
    
    try:
        # Находим новости без sentiment за последние N дней (исключаем EARNINGS/ECONOMIC_INDICATOR — там нет смысла в LLM sentiment)
        cutoff_date = datetime.now() - timedelta(days=days_back)
        
        with engine.connect() as conn:
            query = text("""
                SELECT id, ticker, content, source, event_type
                FROM knowledge_base
                WHERE ts >= :cutoff_date
                  AND sentiment_score IS NULL
                  AND content IS NOT NULL
                  AND LENGTH(content) >= :min_length
                  AND (event_type IS NULL OR event_type NOT IN ('EARNINGS', 'ECONOMIC_INDICATOR'))
                ORDER BY ts DESC
                LIMIT :lim
            """)
            
            params = {
                "cutoff_date": cutoff_date,
                "min_length": min_content_length,
                "lim": limit if limit else 1000
            }
            
            news_df = pd.read_sql(query, conn, params=params)
            
            if news_df.empty:
                logger.info("ℹ️ Нет новостей без sentiment для анализа")
                return
            
            logger.info(f"📊 Найдено {len(news_df)} новостей без sentiment для анализа")
            
            # Обрабатываем батчами (LLM запросы могут быть медленными)
            for i in range(0, len(news_df), batch_size):
                batch = news_df.iloc[i:i+batch_size]
                
                for _, row in batch.iterrows():
                    try:
                        news_id = int(row['id'])
                        content = str(row['content'])
                        ticker = row.get('ticker', 'UNKNOWN')
                        
                        # Рассчитываем sentiment через LLM
                        sentiment_score, insight = calculate_sentiment(content)
                        
                        if sentiment_score is not None:
                            # Обновляем sentiment_score и insight в БД
                            with engine.begin() as conn:
                                update_query = text("""
                                    UPDATE knowledge_base
                                    SET sentiment_score = :sentiment_score, insight = :insight
                                    WHERE id = :news_id
                                """)
                                conn.execute(
                                    update_query,
                                    {
                                        "news_id": news_id,
                                        "sentiment_score": sentiment_score,
                                        "insight": insight if insight and insight.strip() else None
                                    }
                                )
                            
                            updated_count += 1
                            insight_preview = (insight[:80] + "…") if insight and len(insight) > 80 else (insight or "—")
                            logger.info(
                                f"✅ ID={news_id} ({ticker}): sentiment={sentiment_score:.3f}, insight={insight_preview}"
                            )
                        else:
                            skipped_count += 1
                        
                        analyzed_count += 1
                        
                        # Небольшая задержка между запросами к LLM
                        import time
                        time.sleep(0.5)
                        
                    except Exception as e:
                        error_count += 1
                        logger.warning(f"⚠️ Ошибка анализа новости ID={row['id']}: {e}")
                
                logger.info(f"   Обработано {min(i+batch_size, len(news_df))}/{len(news_df)} новостей")
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка добавления sentiment: {e}", exc_info=True)
    
    logger.info("=" * 60)
    logger.info(
        f"✅ Добавление sentiment завершено: "
        f"проанализировано {analyzed_count}, "
        f"обновлено {updated_count}, "
        f"пропущено {skipped_count}, "
        f"ошибок {error_count}"
    )
    logger.info("=" * 60)


def main():
    """Основная функция"""
    # Получаем параметры из переменных окружения
    days_back = int(os.getenv('SENTIMENT_DAYS_BACK', '1'))  # По умолчанию только за сегодня
    limit = None
    if os.getenv('SENTIMENT_LIMIT'):
        try:
            limit = int(os.getenv('SENTIMENT_LIMIT'))
        except ValueError:
            logger.warning(f"⚠️ Неверное значение SENTIMENT_LIMIT: {os.getenv('SENTIMENT_LIMIT')}")
    
    batch_size = int(os.getenv('SENTIMENT_BATCH_SIZE', '10'))  # Меньше батч для LLM
    min_content_length = int(os.getenv('SENTIMENT_MIN_CONTENT_LENGTH', '20'))
    
    add_sentiment_to_news(
        days_back=days_back,
        limit=limit,
        batch_size=batch_size,
        min_content_length=min_content_length
    )


if __name__ == "__main__":
    main()

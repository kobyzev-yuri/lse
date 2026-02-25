#!/usr/bin/env python3
"""
Cron скрипт для анализа исходов событий в knowledge_base.
Анализирует события, которым уже прошло N дней, и обновляет outcome_json.
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

from config_loader import get_database_url
from services.news_impact_analyzer import NewsImpactAnalyzer

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/analyze_event_outcomes.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def analyze_existing_events(
    days_after: int = 7,
    limit: int = None,
    batch_size: int = 50
):
    """
    Анализирует исходы существующих событий в knowledge_base
    
    Args:
        days_after: Минимальное количество дней после события для анализа
        limit: Максимальное количество событий для анализа (если None - все подходящие)
        batch_size: Размер батча для обработки
    """
    logger.info("=" * 60)
    logger.info("🔄 Начало анализа исходов событий")
    logger.info("=" * 60)
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    analyzer = NewsImpactAnalyzer()
    
    analyzed_count = 0
    skipped_count = 0
    error_count = 0
    updated_count = 0
    
    try:
        # Находим события, которые:
        # 1. Произошли не менее N дней назад
        # 2. Ещё не имеют outcome_json
        # 3. Имеют ticker и content
        # 4. Только тикеры, по которым есть котировки в quotes (иначе «Нет котировок» по GOOGL, LOGI и т.д.)
        # 5. Не старше 5 лет
        cutoff_date = datetime.now() - timedelta(days=days_after)
        min_date = datetime.now() - timedelta(days=365 * 5)
        
        with engine.connect() as conn:
            query = text("""
                SELECT kb.id, kb.ticker, kb.ts, kb.event_type, kb.content
                FROM knowledge_base kb
                INNER JOIN (SELECT DISTINCT ticker FROM quotes) q ON q.ticker = kb.ticker
                WHERE kb.ts <= :cutoff_date
                  AND kb.ts >= :min_date
                  AND kb.ticker IS NOT NULL
                  AND kb.content IS NOT NULL
                  AND LENGTH(TRIM(kb.content)) > 10
                  AND (kb.outcome_json IS NULL OR kb.outcome_json::text = 'null'::text)
                ORDER BY kb.ts DESC
                LIMIT :lim
            """)
            
            params = {
                "cutoff_date": cutoff_date,
                "min_date": min_date,
                "lim": limit
            }
            
            events_df = pd.read_sql(query, conn, params=params)
            
            if events_df.empty:
                logger.info("ℹ️ Нет событий для анализа исходов (только тикеры из quotes, события за последние 5 лет, старше %s дн.)", days_after)
                return
            
            logger.info(f"📊 Найдено {len(events_df)} событий для анализа исходов (тикеры есть в quotes, события старше {days_after} дн., не старше 5 лет)")
            
            # Обрабатываем батчами
            for i in range(0, len(events_df), batch_size):
                batch = events_df.iloc[i:i+batch_size]
                
                for _, row in batch.iterrows():
                    try:
                        event_id = int(row['id'])
                        ticker = row['ticker']
                        event_ts = row['ts']
                        
                        # Анализируем исход события
                        outcome = analyzer.analyze_event_outcome(
                            event_id=event_id,
                            ticker=ticker,
                            days_after=days_after,
                            event_ts=event_ts
                        )
                        
                        if outcome:
                            # Обновляем outcome_json
                            success = analyzer.update_event_outcome(event_id, outcome)
                            if success:
                                updated_count += 1
                                logger.debug(
                                    f"✅ Событие ID={event_id} ({ticker}): "
                                    f"изменение {outcome.get('price_change_pct', 0):.2f}%, "
                                    f"исход {outcome.get('outcome', 'UNKNOWN')}"
                                )
                            else:
                                error_count += 1
                        else:
                            skipped_count += 1
                            logger.debug(
                                f"⚠️ Событие ID={event_id} ({ticker}): "
                                f"нет данных о котировках для анализа"
                            )
                        
                        analyzed_count += 1
                        
                    except Exception as e:
                        error_count += 1
                        logger.warning(f"⚠️ Ошибка анализа события ID={row['id']}: {e}")
                
                logger.info(f"   Обработано {min(i+batch_size, len(events_df))}/{len(events_df)} событий")
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка анализа исходов: {e}", exc_info=True)
    
    logger.info("=" * 60)
    logger.info(
        f"✅ Анализ исходов завершен: "
        f"проанализировано {analyzed_count}, "
        f"обновлено {updated_count}, "
        f"пропущено {skipped_count}, "
        f"ошибок {error_count}"
    )
    logger.info("=" * 60)


def main():
    """Основная функция"""
    # Получаем параметры из переменных окружения
    days_after = int(os.getenv('EVENT_OUTCOME_DAYS_AFTER', '7'))
    limit = None
    if os.getenv('EVENT_OUTCOME_LIMIT'):
        try:
            limit = int(os.getenv('EVENT_OUTCOME_LIMIT'))
        except ValueError:
            logger.warning(f"⚠️ Неверное значение EVENT_OUTCOME_LIMIT: {os.getenv('EVENT_OUTCOME_LIMIT')}")
    
    batch_size = int(os.getenv('EVENT_OUTCOME_BATCH_SIZE', '50'))
    
    analyze_existing_events(
        days_after=days_after,
        limit=limit,
        batch_size=batch_size
    )


if __name__ == "__main__":
    main()

"""
Модуль для анализа влияния новостей на движения цены
Анализирует исходы событий: как рынок отреагировал на новости в прошлом
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy import create_engine, text
import numpy as np

from config_loader import get_database_url

logger = logging.getLogger(__name__)


class NewsImpactAnalyzer:
    """
    Анализирует исходы новостей/событий: как изменилась цена после события
    """
    
    def __init__(self):
        """Инициализация NewsImpactAnalyzer"""
        self.db_url = get_database_url()
        self.engine = create_engine(self.db_url)
        logger.info("✅ NewsImpactAnalyzer инициализирован")
    
    def analyze_event_outcome(
        self,
        event_id: int,
        ticker: str,
        days_after: int = 7,
        event_ts: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Анализирует исход события: как изменилась цена после новости
        
        Args:
            event_id: ID события из trade_kb
            ticker: Тикер инструмента
            days_after: Количество дней для анализа после события
            event_ts: Временная метка события (если None - берется из БД)
            
        Returns:
            Словарь с метриками исхода:
            {
                'price_change_pct': float,      # Изменение цены через N дней (%)
                'max_gain_pct': float,          # Максимальный рост (%)
                'max_loss_pct': float,          # Максимальное падение (%)
                'volatility_change': float,     # Изменение волатильности
                'sentiment_match': bool,        # Совпал ли sentiment с движением
                'outcome': str,                 # 'POSITIVE' | 'NEGATIVE' | 'NEUTRAL'
                'days_analyzed': int            # Фактически проанализировано дней
            }
        """
        try:
            # Получаем временную метку события
            if not event_ts:
                with self.engine.connect() as conn:
                    result = conn.execute(
                        text("SELECT ts FROM trade_kb WHERE id = :event_id"),
                        {"event_id": int(event_id)}  # Преобразуем в int для совместимости
                    )
                    row = result.fetchone()
                    if not row:
                        logger.warning(f"⚠️ Событие ID={event_id} не найдено")
                        return None
                    event_ts = row[0]
            
            # Получаем цену на момент события (ближайшую доступную)
            with self.engine.connect() as conn:
                # Ищем котировку на дату события или ближайшую после
                price_query = text("""
                    SELECT date, close, volatility_5
                    FROM quotes
                    WHERE ticker = :ticker
                      AND date >= :event_date
                    ORDER BY date ASC
                    LIMIT 1
                """)
                price_result = conn.execute(
                    price_query,
                    {
                        "ticker": ticker,
                        "event_date": event_ts.date()
                    }
                )
                event_price_row = price_result.fetchone()
                
                if not event_price_row:
                    logger.warning(f"⚠️ Нет котировок для {ticker} на дату события {event_ts.date()}")
                    return None
                
                event_price = float(event_price_row[1])
                event_volatility = float(event_price_row[2]) if event_price_row[2] else None
                
                # Получаем котировки за N дней после события
                end_date = event_ts.date() + timedelta(days=days_after)
                quotes_query = text("""
                    SELECT date, close, volatility_5
                    FROM quotes
                    WHERE ticker = :ticker
                      AND date > :event_date
                      AND date <= :end_date
                    ORDER BY date ASC
                """)
                quotes_df = pd.read_sql(
                    quotes_query,
                    conn,
                    params={
                        "ticker": ticker,
                        "event_date": event_ts.date(),
                        "end_date": end_date
                    }
                )
            
            if quotes_df.empty:
                logger.warning(f"⚠️ Нет котировок для {ticker} после события {event_ts.date()}")
                return None
            
            # Рассчитываем метрики
            final_price = float(quotes_df.iloc[-1]['close'])
            price_change_pct = ((final_price - event_price) / event_price) * 100
            
            # Максимальный рост и падение
            quotes_df['price_change_pct'] = ((quotes_df['close'] - event_price) / event_price) * 100
            max_gain_pct = float(quotes_df['price_change_pct'].max())
            max_loss_pct = float(quotes_df['price_change_pct'].min())
            
            # Изменение волатильности
            volatility_change = None
            if event_volatility and not quotes_df['volatility_5'].isna().all():
                avg_volatility_after = float(quotes_df['volatility_5'].mean())
                volatility_change = ((avg_volatility_after - event_volatility) / event_volatility) * 100 if event_volatility > 0 else 0
            
            # Определяем исход
            if price_change_pct > 2.0:
                outcome = 'POSITIVE'
            elif price_change_pct < -2.0:
                outcome = 'NEGATIVE'
            else:
                outcome = 'NEUTRAL'
            
            # Проверяем совпадение sentiment с движением (если есть sentiment в knowledge_base)
            sentiment_match = None
            try:
                with self.engine.connect() as conn:
                    # Ищем соответствующую новость в knowledge_base
                    sentiment_query = text("""
                        SELECT sentiment_score
                        FROM knowledge_base
                        WHERE ticker = :ticker
                          AND ABS(EXTRACT(EPOCH FROM (ts - :event_ts))) < 3600
                          AND LEFT(content, 100) LIKE :content_prefix
                        LIMIT 1
                    """)
                    # Берем первые 50 символов для поиска
                    content_prefix = quotes_df.iloc[0].get('content', '')[:50] if not quotes_df.empty else ''
                    sentiment_result = conn.execute(
                        sentiment_query,
                        {
                            "ticker": ticker,
                            "event_ts": event_ts,
                            "content_prefix": f"%{content_prefix}%"
                        }
                    )
                    sentiment_row = sentiment_result.fetchone()
                    
                    if sentiment_row and sentiment_row[0] is not None:
                        sentiment_score = float(sentiment_row[0])
                        # Sentiment > 0.5 = положительный, < 0.5 = отрицательный
                        sentiment_positive = sentiment_score > 0.5
                        price_positive = price_change_pct > 0
                        sentiment_match = sentiment_positive == price_positive
            except Exception as e:
                logger.debug(f"Не удалось проверить sentiment match: {e}")
            
            result = {
                'price_change_pct': round(price_change_pct, 2),
                'max_gain_pct': round(max_gain_pct, 2),
                'max_loss_pct': round(max_loss_pct, 2),
                'volatility_change': round(volatility_change, 2) if volatility_change is not None else None,
                'sentiment_match': sentiment_match,
                'outcome': outcome,
                'days_analyzed': len(quotes_df),
                'event_price': round(event_price, 2),
                'final_price': round(final_price, 2)
            }
            
            logger.debug(
                f"✅ Анализ исхода события ID={event_id}: "
                f"изменение {price_change_pct:.2f}%, исход {outcome}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Ошибка анализа исхода события ID={event_id}: {e}")
            return None
    
    def aggregate_patterns(self, similar_events: pd.DataFrame) -> Dict[str, Any]:
        """
        Агрегирует исходы похожих событий
        
        Args:
            similar_events: DataFrame с похожими событиями (должен содержать outcome_json)
            
        Returns:
            Словарь с агрегированными метриками:
            {
                'avg_price_change': float,
                'success_rate': float,  # % случаев, когда sentiment совпал с движением
                'avg_volatility_change': float,
                'typical_outcome': str,
                'confidence': float,  # Насколько уверены в паттерне
                'sample_size': int
            }
        """
        if similar_events.empty:
            return {
                'avg_price_change': 0.0,
                'success_rate': 0.0,
                'avg_volatility_change': 0.0,
                'typical_outcome': 'NEUTRAL',
                'confidence': 0.0,
                'sample_size': 0
            }
        
        # Получаем исходы событий из БД (outcome_json)
        event_ids = similar_events['id'].tolist()
        
        try:
            with self.engine.connect() as conn:
                # Проверяем наличие колонки outcome_json
                result = conn.execute(text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='trade_kb' AND column_name='outcome_json'
                """))
                has_outcome_json = result.fetchone() is not None
                
                if not has_outcome_json:
                    logger.warning("⚠️ Колонка outcome_json отсутствует в trade_kb, анализ исходов невозможен")
                    return {
                        'avg_price_change': 0.0,
                        'success_rate': 0.0,
                        'avg_volatility_change': 0.0,
                        'typical_outcome': 'NEUTRAL',
                        'confidence': 0.0,
                        'sample_size': 0,
                        'note': 'outcome_json column missing'
                    }
                
                # Получаем исходы
                outcomes_query = text("""
                    SELECT outcome_json
                    FROM trade_kb
                    WHERE id = ANY(:event_ids)
                      AND outcome_json IS NOT NULL
                """)
                outcomes_result = conn.execute(
                    outcomes_query,
                    {"event_ids": event_ids}
                )
                
                outcomes = []
                for row in outcomes_result:
                    if row[0]:
                        import json
                        if isinstance(row[0], str):
                            outcomes.append(json.loads(row[0]))
                        else:
                            outcomes.append(row[0])
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения исходов: {e}")
            outcomes = []
        
        if not outcomes:
            logger.info("ℹ️ Нет данных об исходах для похожих событий")
            return {
                'avg_price_change': 0.0,
                'success_rate': 0.0,
                'avg_volatility_change': 0.0,
                'typical_outcome': 'NEUTRAL',
                'confidence': 0.0,
                'sample_size': 0
            }
        
        # Агрегируем метрики
        price_changes = [o.get('price_change_pct', 0) for o in outcomes if o.get('price_change_pct') is not None]
        sentiment_matches = [o.get('sentiment_match') for o in outcomes if o.get('sentiment_match') is not None]
        volatility_changes = [o.get('volatility_change', 0) for o in outcomes if o.get('volatility_change') is not None]
        outcomes_list = [o.get('outcome') for o in outcomes if o.get('outcome')]
        
        avg_price_change = np.mean(price_changes) if price_changes else 0.0
        success_rate = sum(sentiment_matches) / len(sentiment_matches) if sentiment_matches else 0.0
        avg_volatility_change = np.mean(volatility_changes) if volatility_changes else 0.0
        
        # Типичный исход (наиболее частый)
        from collections import Counter
        outcome_counts = Counter(outcomes_list)
        typical_outcome = outcome_counts.most_common(1)[0][0] if outcome_counts else 'NEUTRAL'
        
        # Уверенность (на основе размера выборки и согласованности исходов)
        sample_size = len(outcomes)
        consistency = max(outcome_counts.values()) / sample_size if sample_size > 0 else 0.0
        confidence = min(0.95, consistency * (1 + np.log10(sample_size + 1) / 10))
        
        result = {
            'avg_price_change': round(avg_price_change, 2),
            'success_rate': round(success_rate, 3),
            'avg_volatility_change': round(avg_volatility_change, 2),
            'typical_outcome': typical_outcome,
            'confidence': round(confidence, 3),
            'sample_size': sample_size
        }
        
        logger.info(
            f"📊 Агрегированный анализ паттернов: "
            f"среднее изменение {avg_price_change:.2f}%, "
            f"success rate {success_rate:.1%}, "
            f"уверенность {confidence:.2f} (n={sample_size})"
        )
        
        return result
    
    def update_event_outcome(
        self,
        event_id: int,
        outcome: Dict[str, Any]
    ) -> bool:
        """
        Обновляет outcome_json для события в trade_kb
        
        Args:
            event_id: ID события
            outcome: Словарь с метриками исхода
            
        Returns:
            True если успешно, False при ошибке
        """
        try:
            import json
            
            with self.engine.begin() as conn:
                # Проверяем наличие колонки
                result = conn.execute(text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='trade_kb' AND column_name='outcome_json'
                """))
                if not result.fetchone():
                    # Создаем колонку если её нет
                    conn.execute(text("ALTER TABLE trade_kb ADD COLUMN outcome_json JSONB"))
                    logger.info("✅ Колонка outcome_json добавлена в trade_kb")
                
                # Обновляем outcome_json
                conn.execute(
                    text("""
                        UPDATE trade_kb
                        SET outcome_json = :outcome_json
                        WHERE id = :event_id
                    """),
                    {
                        "event_id": event_id,
                        "outcome_json": json.dumps(outcome)
                    }
                )
            
            logger.debug(f"✅ Исход события ID={event_id} обновлен")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка обновления исхода события ID={event_id}: {e}")
            return False


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Тест
    analyzer = NewsImpactAnalyzer()
    
    # Пример анализа исхода события
    # (требует существующее событие в trade_kb и котировки в quotes)
    # outcome = analyzer.analyze_event_outcome(event_id=1, ticker="MSFT", days_after=7)
    # print(f"Исход события: {outcome}")

"""
Модуль для работы с векторной базой знаний (Vector Knowledge Base)
Использует sentence-transformers для генерации embeddings локально (бесплатно)
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy import create_engine, text
import numpy as np

from config_loader import get_database_url

logger = logging.getLogger(__name__)

# Модель для embeddings (all-mpnet-base-v2: 768 dim, популярная, качественная)
EMBEDDING_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"
EMBEDDING_DIMENSION = 768


class VectorKB:
    """
    Класс для работы с векторной базой знаний
    
    Использует sentence-transformers для генерации embeddings локально (бесплатно).
    Модель: all-mpnet-base-v2 (768 измерений) - популярная модель с хорошим качеством.
    """
    
    def __init__(self):
        """Инициализация VectorKB"""
        self.db_url = get_database_url()
        self.engine = create_engine(self.db_url)
        
        # Ленивая загрузка модели (загружается при первом использовании)
        self._model = None
        self._model_loaded = False
        
        logger.info(f"✅ VectorKB инициализирован (модель: {EMBEDDING_MODEL_NAME}, размерность: {EMBEDDING_DIMENSION})")
    
    def _load_model(self):
        """Загружает модель sentence-transformers (ленивая загрузка)"""
        if self._model_loaded:
            return
        
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"📥 Загрузка модели {EMBEDDING_MODEL_NAME}...")
            self._model = SentenceTransformer(EMBEDDING_MODEL_NAME)
            self._model_loaded = True
            logger.info(f"✅ Модель {EMBEDDING_MODEL_NAME} загружена")
        except ImportError:
            logger.error("❌ sentence-transformers не установлен. Установите: pip install sentence-transformers")
            raise
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки модели: {e}")
            raise
    
    def generate_embedding(self, text: str) -> List[float]:
        """
        Генерирует embedding для текста
        
        Args:
            text: Текст для векторизации
            
        Returns:
            Список из 768 чисел (embedding)
        """
        if not text or not text.strip():
            logger.warning("⚠️ Пустой текст для генерации embedding, возвращаю нулевой вектор")
            return [0.0] * EMBEDDING_DIMENSION
        
        self._load_model()
        
        try:
            # Генерируем embedding
            embedding = self._model.encode(text, normalize_embeddings=True)
            
            # Преобразуем numpy array в список
            embedding_list = embedding.tolist()
            
            if len(embedding_list) != EMBEDDING_DIMENSION:
                logger.error(f"❌ Неверная размерность embedding: {len(embedding_list)}, ожидается {EMBEDDING_DIMENSION}")
                return [0.0] * EMBEDDING_DIMENSION
            
            return embedding_list
        except Exception as e:
            logger.error(f"❌ Ошибка генерации embedding: {e}")
            return [0.0] * EMBEDDING_DIMENSION
    
    def add_event(
        self,
        ticker: str,
        event_type: str,
        content: str,
        ts: datetime,
        source: Optional[str] = None
    ) -> Optional[int]:
        """
        Добавляет событие в trade_kb с embedding
        
        Args:
            ticker: Тикер инструмента
            event_type: Тип события ('NEWS', 'EARNINGS', 'ECONOMIC_INDICATOR', 'TRADE_SIGNAL')
            content: Текст события
            ts: Временная метка
            source: Источник (опционально)
            
        Returns:
            ID добавленной записи или None при ошибке
        """
        if not content or not content.strip():
            logger.warning(f"⚠️ Пустой контент для события {ticker}, пропуск")
            return None
        
        try:
            # Генерируем embedding
            embedding = self.generate_embedding(content)
            
            # Вставляем в БД
            with self.engine.begin() as conn:
                result = conn.execute(
                    text("""
                        INSERT INTO trade_kb (ts, ticker, event_type, content, embedding)
                        VALUES (:ts, :ticker, :event_type, :content, :embedding)
                        RETURNING id
                    """),
                    {
                        "ts": ts,
                        "ticker": ticker,
                        "event_type": event_type,
                        "content": content,
                        "embedding": f"[{','.join(map(str, embedding))}]"  # pgvector формат: [1,2,3,...]
                    }
                )
                event_id = result.fetchone()[0]
                logger.debug(f"✅ Событие добавлено в trade_kb: ID={event_id}, ticker={ticker}, type={event_type}")
                return event_id
        except Exception as e:
            logger.error(f"❌ Ошибка добавления события в trade_kb: {e}")
            return None
    
    def search_similar(
        self,
        query: str,
        ticker: Optional[str] = None,
        limit: int = 5,
        min_similarity: float = 0.5,
        time_window_days: int = 365,
        event_types: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Ищет похожие события через векторный поиск
        
        Args:
            query: Текст запроса для поиска
            ticker: Фильтр по тикеру (если None - все тикеры)
            limit: Максимальное количество результатов
            min_similarity: Минимальная similarity (0.0-1.0)
            time_window_days: Окно поиска в днях (по умолчанию 1 год)
            event_types: Список типов событий для фильтрации (если None - все)
            
        Returns:
            DataFrame с колонками: id, ticker, event_type, content, ts, similarity
        """
        if not query or not query.strip():
            logger.warning("⚠️ Пустой запрос для поиска")
            return pd.DataFrame()
        
        try:
            # Генерируем embedding для запроса
            query_embedding = self.generate_embedding(query)
            
            # Формируем SQL запрос
            where_clauses = []
            params = {
                "query_embedding": f"[{','.join(map(str, query_embedding))}]",  # pgvector формат
                "limit": limit,
                "min_similarity": min_similarity,
                "cutoff_time": datetime.now() - timedelta(days=time_window_days)
            }
            
            # Фильтр по времени
            where_clauses.append("ts >= :cutoff_time")
            
            # Фильтр по тикеру
            if ticker:
                where_clauses.append("(ticker = :ticker OR ticker IN ('MACRO', 'US_MACRO'))")
                params["ticker"] = ticker
            else:
                where_clauses.append("(ticker IS NOT NULL)")
            
            # Фильтр по типам событий
            if event_types:
                placeholders = ','.join([f"'{et}'" for et in event_types])
                where_clauses.append(f"event_type IN ({placeholders})")
            
            where_sql = " AND ".join(where_clauses)
            
            # Векторный поиск через pgvector (cosine distance)
            # Оператор <=> возвращает cosine distance (0 = идентичны, 2 = противоположны)
            # similarity = 1 - distance (1 = идентичны, -1 = противоположны)
            query_sql = f"""
                SELECT 
                    id, ticker, event_type, content, ts,
                    1 - (embedding <=> CAST(:query_embedding AS vector)) as similarity
                FROM trade_kb
                WHERE {where_sql}
                  AND (1 - (embedding <=> CAST(:query_embedding AS vector))) >= :min_similarity
                ORDER BY embedding <=> CAST(:query_embedding AS vector)
                LIMIT :limit
            """
            
            with self.engine.connect() as conn:
                df = pd.read_sql(text(query_sql), conn, params=params)
            
            if df.empty:
                logger.info(f"ℹ️ Похожих событий не найдено для запроса: {query[:50]}...")
            else:
                logger.info(f"✅ Найдено {len(df)} похожих событий (similarity >= {min_similarity:.2f})")
            
            return df
            
        except Exception as e:
            logger.error(f"❌ Ошибка векторного поиска: {e}")
            return pd.DataFrame()
    
    def sync_from_knowledge_base(self, limit: Optional[int] = None, batch_size: int = 100):
        """
        Синхронизирует новости из knowledge_base в trade_kb
        
        Для каждой новости из knowledge_base, которой нет в trade_kb,
        генерирует embedding и добавляет в trade_kb.
        
        Args:
            limit: Максимальное количество новостей для синхронизации (если None - все новые)
            batch_size: Размер батча для обработки
        """
        logger.info("🔄 Начало синхронизации knowledge_base → trade_kb")
        
        synced_count = 0
        skipped_count = 0
        error_count = 0
        
        try:
            with self.engine.connect() as conn:
                # Находим новости из knowledge_base, которых нет в trade_kb
                # Сопоставляем по ticker, event_type, content и ts (примерно)
                query = text("""
                    SELECT DISTINCT ON (kb.id) 
                           kb.id, kb.ts, kb.ticker, kb.source, kb.content,
                           kb.event_type, kb.importance
                    FROM knowledge_base kb
                    WHERE NOT EXISTS (
                        SELECT 1 FROM trade_kb tk
                        WHERE tk.ticker = kb.ticker 
                          AND COALESCE(tk.event_type, 'NEWS') = COALESCE(kb.event_type, 'NEWS')
                          AND ABS(EXTRACT(EPOCH FROM (kb.ts - tk.ts))) < 3600  -- В пределах часа
                          AND LEFT(kb.content, 100) = LEFT(tk.content, 100)  -- Первые 100 символов совпадают
                    )
                      AND kb.content IS NOT NULL
                      AND LENGTH(kb.content) > 10
                    ORDER BY kb.id, kb.ts DESC
                    LIMIT :limit
                """)
                
                params = {"limit": limit if limit else 10000}
                news_df = pd.read_sql(query, conn, params=params)
                
                if news_df.empty:
                    logger.info("ℹ️ Нет новых новостей для синхронизации")
                    return
                
                logger.info(f"📊 Найдено {len(news_df)} новостей для синхронизации")
                
                # Обрабатываем батчами
                for i in range(0, len(news_df), batch_size):
                    batch = news_df.iloc[i:i+batch_size]
                    
                    for _, row in batch.iterrows():
                        try:
                            event_type = row.get('event_type') or 'NEWS'
                            
                            event_id = self.add_event(
                                ticker=row['ticker'],
                                event_type=event_type,
                                content=row['content'],
                                ts=row['ts'],
                                source=row.get('source')
                            )
                            
                            if event_id:
                                synced_count += 1
                            else:
                                skipped_count += 1
                        except Exception as e:
                            error_count += 1
                            logger.warning(f"⚠️ Ошибка синхронизации новости ID={row['id']}: {e}")
                    
                    logger.info(f"   Обработано {min(i+batch_size, len(news_df))}/{len(news_df)} новостей")
                
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации: {e}")
        
        logger.info(
            f"✅ Синхронизация завершена: добавлено {synced_count}, "
            f"пропущено {skipped_count}, ошибок {error_count}"
        )
    
    def get_stats(self) -> Dict[str, int]:
        """
        Возвращает статистику по trade_kb
        
        Returns:
            Словарь с метриками
        """
        try:
            with self.engine.connect() as conn:
                total = conn.execute(text("SELECT COUNT(*) FROM trade_kb")).fetchone()[0]
                with_embedding = conn.execute(
                    text("SELECT COUNT(*) FROM trade_kb WHERE embedding IS NOT NULL")
                ).fetchone()[0]
                
                by_type = {}
                result = conn.execute(
                    text("SELECT event_type, COUNT(*) FROM trade_kb GROUP BY event_type")
                )
                for row in result:
                    by_type[row[0] or 'NULL'] = row[1]
                
                return {
                    'total_events': total,
                    'with_embedding': with_embedding,
                    'by_event_type': by_type
                }
        except Exception as e:
            logger.error(f"❌ Ошибка получения статистики: {e}")
            return {}


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Тест
    vector_kb = VectorKB()
    
    # Тест генерации embedding
    test_text = "Microsoft объявил о росте выручки на 15% в последнем квартале"
    embedding = vector_kb.generate_embedding(test_text)
    print(f"✅ Embedding сгенерирован: размерность {len(embedding)}")
    
    # Тест добавления события
    event_id = vector_kb.add_event(
        ticker="MSFT",
        event_type="NEWS",
        content=test_text,
        ts=datetime.now()
    )
    print(f"✅ Событие добавлено: ID={event_id}")
    
    # Тест поиска
    similar = vector_kb.search_similar("Microsoft выручка рост", ticker="MSFT", limit=3)
    print(f"✅ Найдено похожих событий: {len(similar)}")
    if not similar.empty:
        print(similar[['ticker', 'event_type', 'similarity', 'content']].head())
    
    # Статистика
    stats = vector_kb.get_stats()
    print(f"📊 Статистика: {stats}")

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
from typing import List, Dict, Optional, Tuple, Any
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
        """Загружает модель sentence-transformers (ленивая загрузка). Прокси отключается на время загрузки."""
        if self._model_loaded:
            return
        
        import os
        proxy_vars = (
            "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy", "NO_PROXY", "no_proxy"
        )
        saved = {k: os.environ.pop(k, None) for k in proxy_vars}
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
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
    
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
        source: Optional[str] = None,
        knowledge_base_id: Optional[int] = None,
    ) -> Optional[int]:
        """
        Добавляет событие в knowledge_base с embedding (одна таблица для новостей и векторов).
        
        Args:
            ticker: Тикер инструмента
            event_type: Тип события ('NEWS', 'EARNINGS', 'ECONOMIC_INDICATOR', 'TRADE_SIGNAL')
            content: Текст события
            ts: Временная метка
            source: Источник (сохраняется в БД; по умолчанию 'MANUAL')
            knowledge_base_id: Не используется (оставлен для совместимости API)
            
        Returns:
            ID записи в knowledge_base или None при ошибке
        """
        if not content or not content.strip():
            logger.warning(f"⚠️ Пустой контент для события {ticker}, пропуск")
            return None
        
        try:
            embedding = self.generate_embedding(content)
            src = (source or "MANUAL").strip() or "MANUAL"
            with self.engine.begin() as conn:
                result = conn.execute(
                    text("""
                        INSERT INTO knowledge_base (ts, ticker, source, content, event_type, embedding)
                        VALUES (:ts, :ticker, :source, :content, :event_type, :embedding)
                        RETURNING id
                    """),
                    {
                        "ts": ts,
                        "ticker": ticker,
                        "source": src,
                        "content": content,
                        "event_type": event_type,
                        "embedding": f"[{','.join(map(str, embedding))}]",
                    },
                )
                event_id = result.fetchone()[0]
                logger.debug(f"✅ Событие добавлено в knowledge_base: id={event_id}, ticker={ticker}")
                return event_id
        except Exception as e:
            logger.error(f"❌ Ошибка добавления события в knowledge_base: {e}")
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
                FROM knowledge_base
                WHERE embedding IS NOT NULL AND {where_sql}
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
    
    def count_without_embedding(self) -> int:
        """Возвращает число записей без embedding с подходящим content (для backfill)."""
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT COUNT(*) FROM knowledge_base
                        WHERE embedding IS NULL
                          AND content IS NOT NULL
                          AND TRIM(content) != ''
                          AND LENGTH(TRIM(content)) > 10
                    """)
                ).fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"❌ Ошибка подсчёта записей без embedding: {e}")
            return 0

    def count_total_without_embedding(self) -> int:
        """Возвращает общее число записей без embedding (без фильтра по content)."""
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text("SELECT COUNT(*) FROM knowledge_base WHERE embedding IS NULL")
                ).fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"❌ Ошибка подсчёта: {e}")
            return 0

    def sync_from_knowledge_base(self, limit: Optional[int] = None, batch_size: int = 100):
        """
        Проставляет embedding в knowledge_base для записей, у которых он ещё не заполнен.
        Сначала проверяет, сколько таких записей есть; затем обрабатывает батчами.
        
        Args:
            limit: Максимум записей за запуск (None — обработать все без лимита)
            batch_size: Размер батча
        """
        logger.info("🔄 Backfill embedding: проверка записей без embedding...")

        try:
            # 1. Явная проверка: сколько записей без embedding
            total_without = self.count_total_without_embedding()
            need_count = self.count_without_embedding()
            skipped_content = total_without - need_count
            logger.info(f"📊 Всего без embedding: {total_without}. К обработке (content не пустой, длина > 10): {need_count}")
            if skipped_content > 0:
                logger.info(f"   Пропущено из-за пустого или короткого content (≤10 символов): {skipped_content}")
            if need_count == 0:
                logger.info("ℹ️ Нечего обрабатывать. Завершение.")
                return

            # 2. Выборка для обработки (без лимита по умолчанию; LIMIT NULL в PostgreSQL = все строки)
            with self.engine.connect() as conn:
                query = text("""
                    SELECT id, ticker, content, event_type
                    FROM knowledge_base
                    WHERE embedding IS NULL
                      AND content IS NOT NULL
                      AND TRIM(content) != ''
                      AND LENGTH(TRIM(content)) > 10
                    ORDER BY id
                    LIMIT :lim
                """)
                df = pd.read_sql(query, conn, params={"lim": limit})
            
            to_process = len(df)
            logger.info(f"📊 К обработке в этом запуске: {to_process}" + (f" (лимит {limit})" if limit is not None else " (без лимита)"))
            if to_process == 0:
                return

            updated_count = 0
            error_count = 0
            first_error = None

            for i in range(0, to_process, batch_size):
                batch = df.iloc[i : i + batch_size]
                for _, row in batch.iterrows():
                    try:
                        emb = self.generate_embedding(row["content"])
                        emb_str = f"[{','.join(map(str, emb))}]"
                        with self.engine.begin() as conn:
                            conn.execute(
                                text("UPDATE knowledge_base SET embedding = CAST(:emb AS vector) WHERE id = :id"),
                                {"emb": emb_str, "id": int(row["id"])},
                            )
                        updated_count += 1
                    except Exception as e:
                        error_count += 1
                        if first_error is None:
                            first_error = e
                        logger.warning(f"⚠️ Ошибка backfill id={row['id']}: {e}")
                logger.info(f"   Обработано {min(i + batch_size, to_process)}/{to_process}")
            
            if first_error is not None and error_count > 0:
                logger.warning(f"⚠️ Первая ошибка (для отладки): {first_error}", exc_info=False)
            logger.info(f"✅ Backfill завершён: обновлено {updated_count}, ошибок {error_count}")
        except Exception as e:
            logger.error(f"❌ Ошибка backfill: {e}", exc_info=True)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Возвращает статистику по записям с embedding в knowledge_base.
        """
        try:
            with self.engine.connect() as conn:
                total = conn.execute(text("SELECT COUNT(*) FROM knowledge_base")).fetchone()[0]
                with_embedding = conn.execute(
                    text("SELECT COUNT(*) FROM knowledge_base WHERE embedding IS NOT NULL")
                ).fetchone()[0]
                without_total = self.count_total_without_embedding()
                without_ready = self.count_without_embedding()
                by_type = {}
                result = conn.execute(
                    text("SELECT event_type, COUNT(*) FROM knowledge_base WHERE embedding IS NOT NULL GROUP BY event_type")
                )
                for row in result:
                    by_type[row[0] or "NULL"] = row[1]
                return {
                    "total_events": total,
                    "with_embedding": with_embedding,
                    "without_embedding": without_total,
                    "without_embedding_ready": without_ready,
                    "without_embedding_skipped_content": without_total - without_ready,
                    "by_event_type": by_type,
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

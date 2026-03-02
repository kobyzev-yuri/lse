"""
Модуль для работы с векторной базой знаний (Vector Knowledge Base)
Embeddings: локально (sentence-transformers), через OpenAI (тот же ключ/proxy что GPT-4o) или Gemini.
При Bus error / core dumped используйте USE_OPENAI_EMBEDDINGS=true (рекомендуется при уже настроенном GPT-4o).
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

from config_loader import get_database_url, get_config_value

logger = logging.getLogger(__name__)

# Модель для локальных embeddings (768 dim). Переопределяется через HF_MODEL_NAME в config.env.
DEFAULT_EMBEDDING_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"
EMBEDDING_DIMENSION = 768


def _get_local_embedding_model_name() -> str:
    """Модель для локальных эмбеддингов: из HF_MODEL_NAME или значение по умолчанию."""
    name = (get_config_value("HF_MODEL_NAME") or "").strip()
    return name or DEFAULT_EMBEDDING_MODEL_NAME

# OpenAI: тот же OPENAI_API_KEY и OPENAI_BASE_URL, что для LLM (proxyapi). dimensions=768 для совместимости с БД.
OPENAI_EMBED_MODEL = "text-embedding-3-small"

# Gemini (альтернатива)
GEMINI_EMBED_MODEL = "text-embedding-004"
GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"


def _use_openai_embeddings() -> bool:
    v = get_config_value("USE_OPENAI_EMBEDDINGS") or ""
    return v.strip().lower() in ("1", "true", "yes")


def _use_gemini_embeddings() -> bool:
    v = get_config_value("USE_GEMINI_EMBEDDINGS") or ""
    return v.strip().lower() in ("1", "true", "yes")


def _get_openai_embed_config() -> Tuple[Optional[str], Optional[str]]:
    key = (get_config_value("OPENAI_API_KEY") or "").strip()
    base = (get_config_value("OPENAI_BASE_URL") or "https://api.proxyapi.ru/openai/v1").strip().rstrip("/")
    return (key or None, base or None)


def _get_gemini_api_key() -> Optional[str]:
    key = get_config_value("GEMINI_API_KEY") or ""
    return key.strip() or None


class VectorKB:
    """
    Класс для работы с векторной базой знаний.
    Embeddings: OpenAI (тот же ключ что GPT-4o), Gemini или локально (sentence-transformers).
    """

    def __init__(self):
        """Инициализация VectorKB"""
        self.db_url = get_database_url()
        self.engine = create_engine(self.db_url)

        self._use_openai = _use_openai_embeddings()
        self._openai_key, self._openai_base = _get_openai_embed_config() if self._use_openai else (None, None)
        self._use_gemini = _use_gemini_embeddings()
        self._gemini_key = _get_gemini_api_key() if self._use_gemini else None

        if self._use_openai and not self._openai_key:
            logger.warning("⚠️ USE_OPENAI_EMBEDDINGS=true, но OPENAI_API_KEY не задан — эмбеддинги будут нулевыми")
        if self._use_gemini and not self._gemini_key:
            logger.warning("⚠️ USE_GEMINI_EMBEDDINGS=true, но GEMINI_API_KEY не задан — эмбеддинги будут нулевыми")

        self._model = None
        self._model_loaded = False
        self._local_model_name = _get_local_embedding_model_name()

        if self._use_openai and self._openai_key:
            logger.info(f"✅ VectorKB инициализирован (провайдер: OpenAI, размерность: {EMBEDDING_DIMENSION})")
        elif self._use_gemini and self._gemini_key:
            logger.info(f"✅ VectorKB инициализирован (провайдер: Gemini API, размерность: {EMBEDDING_DIMENSION})")
        else:
            logger.info(f"✅ VectorKB инициализирован (модель: {self._local_model_name}, размерность: {EMBEDDING_DIMENSION})")

    def _embed_openai(self, text: str) -> List[float]:
        """Эмбеддинг через OpenAI API (тот же ключ и base URL что для GPT-4o). dimensions=768 под колонку БД."""
        import requests
        url = f"{self._openai_base}/embeddings"
        payload = {
            "model": OPENAI_EMBED_MODEL,
            "input": text[:8000],  # лимит по токенам
            "dimensions": EMBEDDING_DIMENSION,
        }
        try:
            r = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._openai_key}",
                },
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            emb = data.get("data", [{}])[0].get("embedding")
            if not emb or len(emb) != EMBEDDING_DIMENSION:
                logger.error(f"❌ OpenAI вернул неверный embedding: {len(emb or [])} dim")
                return [0.0] * EMBEDDING_DIMENSION
            arr = np.array(emb, dtype=np.float64)
            norm = np.linalg.norm(arr)
            if norm > 1e-9:
                arr = arr / norm
            return arr.tolist()
        except Exception as e:
            logger.error(f"❌ Ошибка OpenAI Embedding API: {e}")
            return [0.0] * EMBEDDING_DIMENSION

    def _embed_gemini(self, text: str) -> List[float]:
        """Эмбеддинг через Gemini REST API (outputDimensionality=768)."""
        import requests
        url = GEMINI_EMBED_URL.format(model=GEMINI_EMBED_MODEL)
        payload = {
            "content": {"parts": [{"text": text[:20000]}]},  # лимит по длине
            "outputDimensionality": EMBEDDING_DIMENSION,
        }
        try:
            r = requests.post(
                url,
                params={"key": self._gemini_key},
                json=payload,
                timeout=30,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
            values = data.get("embedding", {}).get("values")
            if not values or len(values) != EMBEDDING_DIMENSION:
                logger.error(f"❌ Gemini вернул неверный embedding: {len(values or [])} dim")
                return [0.0] * EMBEDDING_DIMENSION
            # Нормализация как у sentence-transformers (L2)
            arr = np.array(values, dtype=np.float64)
            norm = np.linalg.norm(arr)
            if norm > 1e-9:
                arr = arr / norm
            return arr.tolist()
        except Exception as e:
            logger.error(f"❌ Ошибка Gemini Embedding API: {e}")
            return [0.0] * EMBEDDING_DIMENSION

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
        # Убираем любые переменные окружения со значением socks:// — иначе huggingface/requests падают с "Unable to determine SOCKS version"
        socks_keys = [k for k, v in os.environ.items() if v and "socks" in str(v).lower()]
        for k in socks_keys:
            saved[k] = os.environ.pop(k, None)
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"📥 Загрузка модели {self._local_model_name}...")
            self._model = SentenceTransformer(self._local_model_name)
            self._model_loaded = True
            logger.info(f"✅ Модель {self._local_model_name} загружена")
        except ImportError:
            logger.error("❌ sentence-transformers не установлен. Установите: pip install sentence-transformers")
            raise
        except Exception as e:
            err_msg = str(e)
            if "socks" in err_msg.lower() or "SOCKS" in err_msg:
                logger.error(
                    "❌ Ошибка загрузки модели: %s — отключите прокси (unset HTTP_PROXY HTTPS_PROXY ALL_PROXY) "
                    "или задайте USE_OPENAI_EMBEDDINGS=true в config.env и отключите локальные эмбеддинги.",
                    e,
                )
            else:
                logger.error(f"❌ Ошибка загрузки модели: {e}")
            raise
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def _maybe_e5_prefix(self, text: str, for_query: bool) -> str:
        """Для моделей E5 (multilingual-e5-base и др.) добавляет префикс query: / passage:."""
        if "e5" in self._local_model_name.lower():
            prefix = "query: " if for_query else "passage: "
            return prefix + text if text else text
        return text

    def generate_embedding(self, text: str, for_query: bool = False) -> List[float]:
        """
        Генерирует embedding для текста (OpenAI, Gemini или локальная модель).
        for_query: True для поискового запроса (у E5 — префикс "query: "), False для документов ("passage: ").
        Returns:
            Список из 768 чисел (embedding).
        """
        if not text or not text.strip():
            logger.warning("⚠️ Пустой текст для генерации embedding, возвращаю нулевой вектор")
            return [0.0] * EMBEDDING_DIMENSION

        if self._use_openai and self._openai_key:
            return self._embed_openai(text)
        if self._use_gemini and self._gemini_key:
            return self._embed_gemini(text)

        self._load_model()
        text_to_encode = self._maybe_e5_prefix(text, for_query)

        try:
            embedding = self._model.encode(text_to_encode, normalize_embeddings=True)
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
            # Генерируем embedding для запроса (for_query=True для E5)
            query_embedding = self.generate_embedding(query, for_query=True)
            
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

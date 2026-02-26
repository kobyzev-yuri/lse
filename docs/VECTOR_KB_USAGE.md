# Использование Vector Knowledge Base

## Обзор

Vector Knowledge Base (VectorKB) использует локальные embeddings через `sentence-transformers` для семантического поиска похожих новостей и событий. Это позволяет находить исторические паттерны и анализировать влияние новостей на рынок.

## Модель

**Модель:** `sentence-transformers/all-mpnet-base-v2`
- **Размерность:** 768 измерений
- **Качество:** Лучшее среди общих моделей sentence-transformers
- **Популярность:** 1.1B+ загрузок на Hugging Face
- **Скорость:** Генерация embeddings за миллисекунды
- **Стоимость:** Бесплатно (локальная модель)

## Установка

**Локальная модель (по умолчанию):**

```bash
pip install "transformers>=4.45.0" sentence-transformers>=2.2.0
```

Модель автоматически загрузится при первом использовании (~420 MB). Если при загрузке возникает **Bus error (core dumped)** во всех окружениях — используйте эмбеддинги через Gemini (см. ниже).

**Эмбеддинги через API (без sentence-transformers):**

- **OpenAI (рекомендуется при уже настроенном GPT-4o):** в `config.env` задайте `USE_OPENAI_EMBEDDINGS=true`. Используются тот же `OPENAI_API_KEY` и `OPENAI_BASE_URL` (proxyapi), модель `text-embedding-3-small` с `dimensions=768`.
- **Gemini:** `USE_GEMINI_EMBEDDINGS=true` и `GEMINI_API_KEY=...`. Тогда локальная модель не загружается.

### Окружение (conda) и Bus error / core dumped

Если при загрузке модели (`Load pretrained SentenceTransformer: ...`) во **всех** conda-окружениях возникает **Bus error (core dumped)** или **Segmentation fault**, локальная модель на этой машине не подходит (часто из‑за бинарных расширений PyTorch/tokenizers и CPU).

**Решение без локальной модели — эмбеддинги через API:**

1. **OpenAI (тот же ключ что для GPT-4o):** в `config.env` задайте `USE_OPENAI_EMBEDDINGS=true`. Дополнительный ключ не нужен — используются `OPENAI_API_KEY` и `OPENAI_BASE_URL`.
2. **Или Gemini:** `USE_GEMINI_EMBEDDINGS=true` и `GEMINI_API_KEY=...` ([Google AI Studio](https://aistudio.google.com/apikey)).
3. Перезапустите синхронизацию (`sync_vector_kb_cron.py`). Модель sentence-transformers **не загружается**, эмбеддинги считаются в облаке (768 dim).

- **Python 3.11:** на части машин при загрузке модели возможен *core dumped*. Либо используйте Python 3.10, либо включите `USE_GEMINI_EMBEDDINGS=true`.
- **Python 3.10:** при ошибке `EncoderDecoderCache` обновите transformers: `pip install -U "transformers>=4.45.0"`. Если всё равно Bus error — переходите на Gemini (см. выше).

## Использование

### Базовое использование

```python
from services.vector_kb import VectorKB

# Инициализация
vector_kb = VectorKB()

# Добавление события
event_id = vector_kb.add_event(
    ticker="MSFT",
    event_type="NEWS",
    content="Microsoft объявил о росте выручки на 15%",
    ts=datetime.now()
)

# Поиск похожих событий
similar = vector_kb.search_similar(
    query="Microsoft выручка рост",
    ticker="MSFT",
    limit=5,
    min_similarity=0.5
)
```

### Синхронизация из knowledge_base

```python
# Синхронизировать все новые новости
vector_kb.sync_from_knowledge_base()

# Синхронизировать с лимитом
vector_kb.sync_from_knowledge_base(limit=100, batch_size=50)
```

### Анализ исходов событий

```python
from services.news_impact_analyzer import NewsImpactAnalyzer

analyzer = NewsImpactAnalyzer()

# Анализ исхода конкретного события
outcome = analyzer.analyze_event_outcome(
    event_id=1,
    ticker="MSFT",
    days_after=7
)

# Обновление исхода в БД
analyzer.update_event_outcome(event_id=1, outcome=outcome)

# Агрегация паттернов похожих событий
patterns = analyzer.aggregate_patterns(similar_events_df)
```

## Cron скрипт

Автоматическая синхронизация через cron:

```bash
# Добавить в crontab (например, раз в день в 3:00)
0 3 * * * cd /path/to/lse && python scripts/sync_vector_kb_cron.py >> logs/sync_vector_kb.log 2>&1
```

Переменные окружения:
- `VECTOR_KB_SYNC_LIMIT` - лимит новостей для синхронизации (по умолчанию: все)
- `VECTOR_KB_BATCH_SIZE` - размер батча (по умолчанию: 100)

## Структура БД

Векторный поиск и исходы событий хранятся в той же таблице **knowledge_base** (одна таблица для новостей и эмбеддингов). В ней есть колонки:

- **embedding** `vector(768)` — для семантического поиска (sentence-transformers); NULL, пока не посчитан
- **outcome_json** `JSONB` — результат анализа исхода события (цена через N дней и т.д.)

Остальные колонки: id, ts, ticker, source, content, sentiment_score, event_type, insight, link, importance. Создание таблицы и добавление колонок — в `init_db.py`.

### Индекс для векторного поиска

Индекс **kb_embedding_idx** создаётся в init_db при наличии минимум 10 записей с `embedding IS NOT NULL`:

```sql
CREATE INDEX IF NOT EXISTS kb_embedding_idx
ON knowledge_base USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100)
WHERE embedding IS NOT NULL;
```

## API методы

### VectorKB

- `generate_embedding(text: str) -> List[float]` - Генерация embedding для текста
- `add_event(ticker, event_type, content, ts, source=None) -> int` - Добавление события
- `search_similar(query, ticker=None, limit=5, min_similarity=0.5, ...) -> pd.DataFrame` - Поиск похожих событий
- `sync_from_knowledge_base(limit=None, batch_size=100)` - Backfill embedding в knowledge_base (UPDATE для записей с embedding IS NULL)
- `get_stats() -> Dict` - Статистика по knowledge_base (всего записей, с embedding, по типам)

### NewsImpactAnalyzer

- `analyze_event_outcome(event_id, ticker, days_after=7) -> Dict` - Анализ исхода события
- `aggregate_patterns(similar_events: pd.DataFrame) -> Dict` - Агрегация паттернов
- `update_event_outcome(event_id, outcome: Dict) -> bool` - Обновление исхода в БД

## Интеграция в торговую систему

### Использование в AnalystAgent

```python
from services.vector_kb import VectorKB
from services.news_impact_analyzer import NewsImpactAnalyzer

vector_kb = VectorKB()
analyzer = NewsImpactAnalyzer()

# Поиск похожих исторических событий
similar_events = vector_kb.search_similar(
    query=current_news_content,
    ticker=ticker,
    limit=10,
    time_window_days=365
)

# Анализ паттернов
if not similar_events.empty:
    patterns = analyzer.aggregate_patterns(similar_events)
    
    # Использовать patterns для принятия решения:
    # - patterns['avg_price_change'] - среднее изменение цены
    # - patterns['success_rate'] - % совпадения sentiment с движением
    # - patterns['confidence'] - уверенность в паттерне
```

## Производительность

- **Генерация embedding:** ~10-50ms на текст (зависит от длины)
- **Векторный поиск:** ~10-100ms (зависит от размера БД и наличия индекса)
- **Синхронизация:** ~100-500 событий/минуту (зависит от скорости генерации embeddings)

## Ограничения

1. **Размерность:** 768 измерений (не 1536 как у OpenAI)
2. **Язык:** Модель поддерживает английский лучше всего (но работает и с другими языками)
3. **Контекст:** Максимальная длина текста ~384 токена (для более длинных текстов используется усечение)
4. **Индекс:** Требуется минимум 10 записей для создания ivfflat индекса

## Миграция с OpenAI embeddings

Если ранее использовались OpenAI embeddings (1536 dim):

1. В **knowledge_base** колонка `embedding` имеет размерность 768 (init_db / миграция).
2. Новые embeddings генерируются локально через sentence-transformers; backfill — скрипт `sync_vector_kb_cron.py`.

## Отладка

Включить детальное логирование:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

Проверить статистику:

```python
stats = vector_kb.get_stats()
print(stats)
```

## Следующие шаги

1. ✅ Базовая функциональность реализована
2. ⏳ Интеграция в AnalystAgent (get_historical_context)
3. ⏳ Автоматический анализ исходов для новых событий
4. ⏳ Использование паттернов в торговых стратегиях

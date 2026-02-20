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

```bash
pip install sentence-transformers>=2.2.0
```

Модель автоматически загрузится при первом использовании (~420 MB).

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

### Таблица `trade_kb`

```sql
CREATE TABLE trade_kb (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP,
    ticker VARCHAR(10),
    event_type VARCHAR(50),  -- 'NEWS', 'EARNINGS', 'ECONOMIC_INDICATOR', 'TRADE_SIGNAL'
    content TEXT,
    embedding vector(768),   -- sentence-transformers embeddings
    outcome_json JSONB       -- Результаты анализа исхода события
);
```

### Индекс для векторного поиска

Индекс `ivfflat` создается автоматически при наличии минимум 10 записей:

```sql
CREATE INDEX trade_kb_embedding_idx 
ON trade_kb 
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);
```

## API методы

### VectorKB

- `generate_embedding(text: str) -> List[float]` - Генерация embedding для текста
- `add_event(ticker, event_type, content, ts, source=None) -> int` - Добавление события
- `search_similar(query, ticker=None, limit=5, min_similarity=0.5, ...) -> pd.DataFrame` - Поиск похожих событий
- `sync_from_knowledge_base(limit=None, batch_size=100)` - Синхронизация из knowledge_base
- `get_stats() -> Dict` - Статистика по trade_kb

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

1. Таблица `trade_kb` автоматически мигрируется при запуске `init_db.py`
2. Старые embeddings будут потеряны (но таблица обычно пустая на старте)
3. Новые embeddings будут генерироваться локально через sentence-transformers

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

# Автоматизация анализа новостей и исходов событий

## Обзор

Три скрипта для автоматизации анализа новостей и исходов событий:

1. **`scripts/analyze_event_outcomes_cron.py`** - Анализ исходов событий (как изменилась цена после новости)
2. **`scripts/add_sentiment_to_news_cron.py`** - Добавление sentiment анализа к новостям без sentiment
3. **`scripts/sync_vector_kb_cron.py`** - Синхронизация новостей в Vector KB (уже существует)

---

## 1. Анализ исходов событий

### Описание

Анализирует события из `trade_kb`, которым уже прошло N дней, и обновляет `outcome_json` с результатами:
- Изменение цены через N дней (%)
- Максимальный рост/падение
- Изменение волатильности
- Совпадение sentiment с движением цены
- Типичный исход (POSITIVE/NEGATIVE/NEUTRAL)

### Запуск вручную

```bash
# Анализировать события, которым прошло 7+ дней
python scripts/analyze_event_outcomes_cron.py

# С параметрами через переменные окружения
EVENT_OUTCOME_DAYS_AFTER=7 EVENT_OUTCOME_LIMIT=100 python scripts/analyze_event_outcomes_cron.py
```

### Переменные окружения

- `EVENT_OUTCOME_DAYS_AFTER` - Минимальное количество дней после события для анализа (по умолчанию: 7)
- `EVENT_OUTCOME_LIMIT` - Максимальное количество событий для анализа (по умолчанию: все подходящие)
- `EVENT_OUTCOME_BATCH_SIZE` - Размер батча для обработки (по умолчанию: 50)

### Cron job

```bash
# Добавить в crontab (например, раз в день в 4:00)
0 4 * * * cd /path/to/lse && python scripts/analyze_event_outcomes_cron.py >> logs/analyze_event_outcomes.log 2>&1
```

### Логи

Логи сохраняются в `logs/analyze_event_outcomes.log`

---

## 2. Добавление sentiment анализа к новостям

### Описание

Добавляет sentiment анализ через LLM к новостям из RSS и NewsAPI, которые не имеют sentiment_score.

**Требования:**
- `USE_LLM=true` в `config.env`
- Настроенный LLM сервис (proxyapi.ru, Groq, Ollama и т.д.)

### Запуск вручную

```bash
# Анализировать новости за последний день
python scripts/add_sentiment_to_news_cron.py

# С параметрами
SENTIMENT_DAYS_BACK=3 SENTIMENT_LIMIT=50 python scripts/add_sentiment_to_news_cron.py
```

### Переменные окружения

- `SENTIMENT_DAYS_BACK` - Анализировать новости за последние N дней (по умолчанию: 1)
- `SENTIMENT_LIMIT` - Максимальное количество новостей для анализа (по умолчанию: 1000)
- `SENTIMENT_BATCH_SIZE` - Размер батча для обработки (по умолчанию: 10, меньше из-за LLM)
- `SENTIMENT_MIN_CONTENT_LENGTH` - Минимальная длина контента для анализа (по умолчанию: 20)

### Cron job

```bash
# Добавить в crontab (например, раз в день в 2:00, после fetch_news_cron)
0 2 * * * cd /path/to/lse && python scripts/add_sentiment_to_news_cron.py >> logs/add_sentiment_to_news.log 2>&1
```

**Важно:** Запускать после `fetch_news_cron.py`, чтобы новые новости успели сохраниться.

### Логи

Логи сохраняются в `logs/add_sentiment_to_news.log`

### Ограничения

- LLM запросы могут быть медленными (задержка 0.5 сек между запросами)
- Учитывайте лимиты и стоимость LLM API
- Рекомендуется ограничивать `SENTIMENT_LIMIT` для экономии ресурсов

---

## 3. Синхронизация Vector KB

### Описание

Синхронизирует новости из `knowledge_base` в `trade_kb` с генерацией embeddings.

### Запуск

```bash
python scripts/sync_vector_kb_cron.py
```

### Cron job

```bash
# Раз в день в 3:00
0 3 * * * cd /path/to/lse && python scripts/sync_vector_kb_cron.py >> logs/sync_vector_kb.log 2>&1
```

---

## Рекомендуемый порядок запуска cron jobs

```bash
# 1. Получение новостей (существующий cron)
0 1 * * * cd /path/to/lse && python scripts/fetch_news_cron.py >> logs/fetch_news.log 2>&1

# 2. Добавление sentiment к новостям (новый)
0 2 * * * cd /path/to/lse && python scripts/add_sentiment_to_news_cron.py >> logs/add_sentiment_to_news.log 2>&1

# 3. Синхронизация Vector KB (существующий)
0 3 * * * cd /path/to/lse && python scripts/sync_vector_kb_cron.py >> logs/sync_vector_kb.log 2>&1

# 4. Анализ исходов событий (новый)
0 4 * * * cd /path/to/lse && python scripts/analyze_event_outcomes_cron.py >> logs/analyze_event_outcomes.log 2>&1
```

**Логика:**
1. Сначала получаем новые новости
2. Добавляем sentiment к новостям без sentiment
3. Синхронизируем в Vector KB с embeddings
4. Анализируем исходы событий, которым уже прошло достаточно времени

---

## Первоначальный запуск

### Шаг 1: Анализ исходов для существующих событий

```bash
# Анализировать все события старше 7 дней
EVENT_OUTCOME_DAYS_AFTER=7 python scripts/analyze_event_outcomes_cron.py
```

Это заполнит `outcome_json` для существующих событий в `trade_kb`.

### Шаг 2: Добавление sentiment к недавним новостям

```bash
# Добавить sentiment к новостям за последние 7 дней
SENTIMENT_DAYS_BACK=7 SENTIMENT_LIMIT=100 python scripts/add_sentiment_to_news_cron.py
```

Это добавит sentiment к новостям из RSS и NewsAPI.

### Шаг 3: Проверка результатов

```python
from sqlalchemy import create_engine, text
from config_loader import get_database_url

engine = create_engine(get_database_url())
with engine.connect() as conn:
    # Проверяем события с исходами
    result = conn.execute(text("""
        SELECT COUNT(*) 
        FROM trade_kb 
        WHERE outcome_json IS NOT NULL
    """))
    print(f"Событий с исходами: {result.fetchone()[0]}")
    
    # Проверяем новости с sentiment
    result = conn.execute(text("""
        SELECT COUNT(*) 
        FROM knowledge_base 
        WHERE sentiment_score IS NOT NULL
    """))
    print(f"Новостей с sentiment: {result.fetchone()[0]}")
```

---

## Мониторинг

### Проверка статуса

```bash
# Проверить последние логи
tail -50 logs/analyze_event_outcomes.log
tail -50 logs/add_sentiment_to_news.log
tail -50 logs/sync_vector_kb.log
```

### Статистика по БД

```python
from services.vector_kb import VectorKB
from sqlalchemy import create_engine, text
from config_loader import get_database_url

# Статистика Vector KB
vector_kb = VectorKB()
stats = vector_kb.get_stats()
print(f"Событий в trade_kb: {stats.get('total_events', 0)}")
print(f"С событиями с исходами: {stats.get('with_outcome', 0)}")

# Статистика sentiment
engine = create_engine(get_database_url())
with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT 
            COUNT(*) as total,
            COUNT(sentiment_score) as with_sentiment,
            COUNT(*) FILTER (WHERE sentiment_score IS NULL) as without_sentiment
        FROM knowledge_base
        WHERE ts >= NOW() - INTERVAL '7 days'
    """))
    row = result.fetchone()
    print(f"Новостей за 7 дней: {row[0]}")
    print(f"С sentiment: {row[1]}")
    print(f"Без sentiment: {row[2]}")
```

---

## Устранение проблем

### Ошибка "USE_LLM=false"

Если `add_sentiment_to_news_cron.py` не работает:
1. Проверьте `config.env`: `USE_LLM=true`
2. Проверьте настройки LLM сервиса
3. Проверьте доступность LLM API

### Ошибка "Нет данных о котировках"

Если `analyze_event_outcomes_cron.py` пропускает события:
1. Убедитесь, что есть котировки в `quotes` для тикеров
2. Проверьте, что события произошли достаточно давно (минимум `EVENT_OUTCOME_DAYS_AFTER` дней)
3. Проверьте соответствие тикеров между `trade_kb` и `quotes`

### Медленная работа

- Уменьшите `SENTIMENT_BATCH_SIZE` для LLM запросов
- Уменьшите `EVENT_OUTCOME_BATCH_SIZE` для анализа исходов
- Используйте `LIMIT` для ограничения количества обрабатываемых записей

---

## Связанные файлы

- `scripts/analyze_event_outcomes_cron.py` - Анализ исходов событий
- `scripts/add_sentiment_to_news_cron.py` - Добавление sentiment
- `scripts/sync_vector_kb_cron.py` - Синхронизация Vector KB
- `services/news_impact_analyzer.py` - Модуль анализа исходов
- `services/sentiment_analyzer.py` - Модуль sentiment анализа
- `services/vector_kb.py` - Модуль Vector KB

# Поля knowledge_base: назначение и кто заполняет

## Почему embedding и другие поля пустые

Записи в `knowledge_base` создаются **разными процессами в разное время**:

1. **Сбор новостей** (cron `fetch_news_cron.py`, ручной импорт) — создаёт строки с `ts`, `ticker`, `source`, `content`, `event_type`, часто без `sentiment_score`, без `insight`, без `embedding`, без `outcome_json`.
2. **Заполнение опциональных полей** делают **отдельные cron-скрипты**. Если они не запускались или не добавлены в расписание — поля так и остаются NULL.

Ниже — полный список колонок, кто их заполняет и как добиться заполнения.

---

## Колонки таблицы knowledge_base

| Колонка | Тип | Назначение | Кто заполняет | Когда бывает пустым |
|--------|-----|------------|----------------|----------------------|
| **id** | SERIAL | Первичный ключ | БД | — |
| **ts** | TIMESTAMP | Время события/публикации | Источник новостей (RSS, AV, NewsAPI, парсеры) | Редко |
| **ticker** | VARCHAR(10) | Тикер (MSFT, US_MACRO, MACRO) | Источник новостей | Макро без тикера — MACRO/US_MACRO |
| **source** | VARCHAR(100) | Источник (Alpha Vantage, RSS, NewsAPI и т.д.) | Источник новостей | — |
| **content** | TEXT | Текст новости/события | Источник новостей | Не должно быть пустым для поиска |
| **sentiment_score** | DECIMAL(3,2) | Тональность 0–1 (или нормализованная -1…1 в логике) | Alpha Vantage News — при сохранении; для остальных — **add_sentiment_to_news_cron.py** (LLM) | У RSS, NewsAPI, Earnings, Economic — пока не отработает cron с LLM |
| **insight** | TEXT | Краткий вывод по новости (ключевой факт) | Стратегии (AnalystAgent) при анализе; **add_sentiment_to_news_cron.py** (LLM) | Большинство записей, пока не считано через LLM |
| **event_type** | VARCHAR(50) | NEW, EARNINGS, ECONOMIC_INDICATOR, FOMC_STATEMENT и т.д. | Источник новостей | Старые записи до миграции |
| **importance** | VARCHAR(10) | HIGH, MEDIUM, LOW | Источник новостей / парсеры | Не все источники передают |
| **link** | TEXT | URL новости | Alpha Vantage News, NewsAPI, RSS | У календарей и экономических индикаторов нет |
| **region** | VARCHAR(20) | USA, UK, EU, Japan и т.д. | RSS, NewsAPI, investing_calendar_parser | Добавляется миграцией; не все источники заполняют |
| **embedding** | vector(768) | Вектор для семантического поиска (sentence-transformers) | **sync_vector_kb_cron.py** (backfill) или VectorKB.add_event() при ручном добавлении | У всех записей, созданных сборщиками новостей, пока не отработает sync_vector_kb_cron |
| **outcome_json** | JSONB | Исход события: изменение цены через N дней, метка (UP/DOWN/FLAT) | **analyze_event_outcomes_cron.py** (NewsImpactAnalyzer) | У всех записей, пока не отработает анализ исходов (и только для событий с достаточной историей котировок) |

---

## Как заполнять поля

### embedding (векторный поиск, /ask в боте)

- **Скрипт:** `scripts/sync_vector_kb_cron.py`  
- **Действие:** выбирает строки с `embedding IS NULL` и `content` не пустой, считает вектор по `content` (модель sentence-transformers) и делает `UPDATE knowledge_base SET embedding = ...`.  
- **Запуск вручную:**  
  `python scripts/sync_vector_kb_cron.py`  
- **Cron (рекомендуется после сбора новостей):**  
  `0 3 * * * cd /path/to/lse && python scripts/sync_vector_kb_cron.py >> logs/sync_vector_kb.log 2>&1`  
- **Ограничение:** для генерации embedding нужна установленная модель sentence-transformers (~420 MB при первом запуске).

### sentiment_score и insight (тон и краткий вывод)

- **Скрипт:** `scripts/add_sentiment_to_news_cron.py`  
- **Действие:** находит записи без `sentiment_score` (за последние N дней, из RSS/NewsAPI/MANUAL), вызывает LLM (sentiment_analyzer), пишет `sentiment_score` и при наличии — `insight`.  
- **Условие:** в config должно быть `USE_LLM=true` и настроен LLM (OpenAI-совместимый API). Модель берётся из `OPENAI_MODEL` (параметр `SENTIMENT_LLM_MODEL` в коде не используется).  
- **Запуск вручную:**  
  `SENTIMENT_DAYS_BACK=7 SENTIMENT_LIMIT=100 python scripts/add_sentiment_to_news_cron.py`  
- **Cron:**  
  `0 2 * * * cd /path/to/lse && python scripts/add_sentiment_to_news_cron.py >> logs/add_sentiment_to_news.log 2>&1`  
- **Примечание:** Alpha Vantage News Sentiment при сохранении уже проставляет `sentiment_score`; cron нужен для RSS, NewsAPI и других источников без sentiment.

**Параметры конфига, связанные с LLM для sentiment:**

| Параметр | Назначение |
|----------|------------|
| `SENTIMENT_AUTO_CALCULATE=true` | При добавлении новостей через CLI (`news_importer.py`), CSV или веб-интерфейс — сразу вызывать LLM и заполнять `sentiment_score` и `insight`. Не влияет на cron. |
| `USE_LLM=true` | Разрешить скрипту **add_sentiment_to_news_cron.py** вызывать LLM (backfill по новостям без sentiment). Если `false`, cron выходит без запросов к API. |
| `OPENAI_MODEL` | Какая модель используется для всех вызовов LLM, в т.ч. для sentiment (например `gpt-4o`). |
| `SENTIMENT_LLM_MODEL` | В коде **не читается** — отдельная модель только для sentiment не задаётся. |

**Ориентировочная стоимость LLM за неделю (sentiment + insight):**

- Один вызов: системный промпт ~100 токенов, новость (content) ~100–400 токенов, ответ до 150 токенов. Итого **~300–500 входных + ~80–150 выходных токенов** на одну новость.
- Объём за неделю: скрипт по умолчанию берёт до **1000** новостей без sentiment за `SENTIMENT_DAYS_BACK=7`. Реальный приток зависит от количества тикеров и источников (RSS, NewsAPI) — часто **50–300 новостей/неделю**.
- Тарифы (ориентир): OpenAI GPT-4o — $2.50/1M вход, $10/1M выход. Прокси (proxyapi.ru и аналоги) могут быть дороже — уточняйте на сайте провайдера.
- **Оценка при 200 новостях/неделю:** ~$0.35–0.70 (OpenAI) или примерно **$0.50–1.50** при наценке прокси. При 500 новостях — примерно **$1–3** за неделю. Ограничение `SENTIMENT_LIMIT=200` уменьшает и объём, и стоимость.

### outcome_json (исход события для анализа влияния новостей)

- **Скрипт:** `scripts/analyze_event_outcomes_cron.py`  
- **Действие:** для событий старше N дней без `outcome_json` считает изменение цены тикера после даты события, формирует JSON (price_change_pct, outcome: UP/DOWN/FLAT и т.д.) и обновляет `outcome_json` в knowledge_base.  
- **Запуск вручную:**  
  `EVENT_OUTCOME_DAYS_AFTER=7 python scripts/analyze_event_outcomes_cron.py`  
- **Cron:**  
  `0 4 * * * cd /path/to/lse && python scripts/analyze_event_outcomes_cron.py >> logs/analyze_event_outcomes.log 2>&1`  
- **Ограничение:** нужны котировки в `quotes` до и после даты события по данному тикеру; для макро (MACRO/US_MACRO) логика может быть другой (например, по индексу).

---

## Порядок запуска для полного заполнения

1. **Сбор новостей:**  
   `python scripts/fetch_news_cron.py` — заполняются ts, ticker, source, content, event_type, importance, link/region где есть.

2. **Backfill embedding:**  
   `python scripts/sync_vector_kb_cron.py` — заполняется embedding для записей с непустым content.

3. **Sentiment и insight (если нужны и включён LLM):**  
   `python scripts/add_sentiment_to_news_cron.py` — заполняются sentiment_score и insight для подходящих записей.

4. **Анализ исходов (для аналитики влияния новостей):**  
   `python scripts/analyze_event_outcomes_cron.py` — заполняется outcome_json для событий с достаточной историей.

Если эти скрипты не ставятся в cron, то **embedding**, **sentiment_score**, **insight** и **outcome_json** так и останутся пустыми у большей части записей.

---

## Проверка заполненности

```sql
SELECT
  COUNT(*) AS total,
  COUNT(embedding) AS with_embedding,
  COUNT(sentiment_score) AS with_sentiment,
  COUNT(insight) AS with_insight,
  COUNT(outcome_json) AS with_outcome
FROM knowledge_base;
```

См. также: [NEWS.md](NEWS.md), [VECTOR_KB_USAGE.md](VECTOR_KB_USAGE.md).

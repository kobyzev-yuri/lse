# Новости и события: одна таблица knowledge_base

**Архитектура:** одна таблица **knowledge_base** — новости, sentiment, источник, плюс опционально **embedding** (вектор 768) и **outcome_json** (исход события). Таблица trade_kb удалена; миграция описана в [KNOWLEDGE_BASE_SINGLE_TABLE.md](KNOWLEDGE_BASE_SINGLE_TABLE.md).

---

## В двух словах

| Что | Где |
|-----|-----|
| Все новости/события | **knowledge_base** (content, source, sentiment_score, event_type, insight, link, importance, **embedding**, **outcome_json**) |
| Векторный поиск | `knowledge_base` с `WHERE embedding IS NOT NULL` |
| Анализ исходов | обновление **outcome_json** в той же строке |
| `/news`, get_recent_news | только **knowledge_base** |

**Поток:** источники (RSS, API, импорт) пишут в knowledge_base. Крон `sync_vector_kb_cron.py` проставляет **embedding** для записей, у которых он ещё NULL (backfill). Отдельной второй таблицы нет.

---

## 1. knowledge_base

**Что хранится:** одна запись = одна новость/событие.

- **ts** — время
- **ticker** — тикер (MSFT, GC=F, MACRO, US_MACRO и т.д.)
- **source** — источник (RSS, NewsAPI, Alpha Vantage, ECB, MANUAL и т.д.)
- **content** — текст новости
- **sentiment_score** — оценка тональности (если есть)
- **event_type** — тип: NEWS, EARNINGS, ECONOMIC_INDICATOR и т.д.
- **insight**, **link**, **importance** — по необходимости
- **embedding** — вектор 768 для семантического поиска (NULL, пока не посчитан; backfill через `sync_vector_kb_cron.py`)
- **outcome_json** — результат анализа исхода события (цена через N дней и т.д.)

**Как пополняется:**

| Способ | Скрипт/модуль | Когда |
|--------|----------------|-------|
| RSS (ЦБ: Fed, ECB, BoE, BoJ) | `services/rss_news_fetcher.py` | По крону: `fetch_news_cron.py` (каждый час) |
| Alpha Vantage (новости, sentiment) | `services/alphavantage_fetcher.py` | Тот же cron |
| NewsAPI | `services/newsapi_fetcher.py` | Тот же cron |
| Календарь Investing.com | `services/investing_calendar_parser.py` | Тот же cron |
| Ручной импорт (CSV/JSON) | `news_importer.py` | По необходимости |

Итог: **все источники пишут в knowledge_base.** Команда `/news`, векторный поиск (`/ask`), анализ исходов работают с этой же таблицей.

---

## 2. Backfill embedding

Записи без **embedding** (новые или старые) обрабатываются крон-скриптом **sync_vector_kb_cron.py**: он вызывает `VectorKB.sync_from_knowledge_base()`, который выбирает из knowledge_base строки с `embedding IS NULL` и проставляет embedding (UPDATE). Ручные события можно добавлять через `VectorKB.add_event()` — они пишутся в knowledge_base с `source='MANUAL'` и сразу с embedding.

---

## 3. Схема потока данных

```
Источники новостей (RSS, NewsAPI, Alpha Vantage, импорт)
        │
        ▼
  knowledge_base  ◄── единственная таблица (content, source, sentiment, embedding, outcome_json)
        │
        │  sync_vector_kb_cron.py: backfill embedding где NULL
        │
        ├── /news, get_recent_news: читают knowledge_base
        └── search_similar, /ask: читают knowledge_base WHERE embedding IS NOT NULL
```

---

## 4. Миграция с существующей trade_kb

Если у вас уже была таблица **trade_kb**, один раз выполните:

```bash
python scripts/migrate_trade_kb_to_knowledge_base.py
```

Скрипт копирует embedding и outcome_json в knowledge_base и удаляет trade_kb. После этого обновлённый код и init_db используют только knowledge_base.

---

## 5. Практические выводы

- **Единый источник правды** — **knowledge_base**. Все новости и события хранятся здесь; embedding и outcome_json — опциональные поля в той же таблице.
- **Добавление новости:** через `news_importer.py`, RSS/API-фетчеры или `VectorKB.add_event()` (с source='MANUAL') — всё пишется в knowledge_base.

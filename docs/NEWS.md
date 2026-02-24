# Новости: источники, таблица knowledge_base, скрипты

Одна таблица **knowledge_base** — все новости/события, опционально `embedding` (векторный поиск) и `outcome_json` (исход события). Источники пишут в неё через cron; backfill embedding и sentiment — отдельными скриптами.

---

## 1. Источники

| Источник | Реализовано | Работает | Ограничения |
|----------|-------------|----------|-------------|
| **RSS ЦБ** (Fed, BoE, ECB, BoJ) | Да | Да | — |
| **NewsAPI** | Да | Да (ключ) | 100 запросов/день |
| **Alpha Vantage** (Earnings + News Sentiment) | Да | Да (ключ) | ~25 запросов/день |
| **Alpha Vantage** (Economic) | Код есть | Нет | В cron выключено; free tier часто пусто |
| **Investing.com** | Код есть | Нет | Таблица через JS, парсер получает пустой HTML |
| **LLM (GPT/Gemini и т.д.)** | Да | Опционально | Прямой запрос к LLM: «какие новости влияют на тикер»; при `USE_LLM_NEWS=true` и настроенном `OPENAI_API_KEY` cron сохраняет ответ в knowledge_base как одна запись с `source='LLM (model)'`. По умолчанию тикер SNDK; список задаётся `LLM_NEWS_TICKERS=SNDK,MU`. |

**Запуск сбора:** `python scripts/fetch_news_cron.py`. В `config.env`: `ALPHAVANTAGE_KEY`, `NEWSAPI_KEY`. Для LLM-новостей: `USE_LLM_NEWS=true`, `OPENAI_API_KEY` (и при необходимости `OPENAI_BASE_URL`, `OPENAI_MODEL`); опционально `LLM_NEWS_TICKERS=SNDK`.

**Проверка:**  
`SELECT source, COUNT(*), MIN(ts), MAX(ts) FROM knowledge_base GROUP BY source ORDER BY 2 DESC;`

---

## 2. FAQ

- **MANUAL** — записи, добавленные через `VectorKB.add_event()` без указания `source` (по умолчанию `'MANUAL'`). Не «ручной импорт новостей», а события из кода/бота.
- **Почему нет строки «NewsAPI» в списке source?** В БД сохраняется название издания из API (Bloomberg, Reuters, The Globe and Mail и т.д.) — это и есть новости из NewsAPI.
- **Чего не хватает:** стабильного экономического календаря (даты CPI, NFP и т.д.) и числовых макро-рядов по регионам. Для базовой работы (новости ЦБ, макро-новости, earnings) текущих источников достаточно.

---

## 3. Скрипты

| Скрипт | Назначение |
|--------|------------|
| `scripts/fetch_news_cron.py` | Сбор из RSS, NewsAPI, Alpha Vantage, при `USE_LLM_NEWS=true` — LLM-запрос по тикерам из `LLM_NEWS_TICKERS` (по умолч. SNDK). |
| `scripts/sync_vector_kb_cron.py` | Backfill `embedding` для записей с `embedding IS NULL`. |
| `scripts/add_sentiment_to_news_cron.py` | LLM: заполнение `sentiment_score` и `insight` для новостей без sentiment. |
| `scripts/analyze_event_outcomes_cron.py` | Заполнение `outcome_json` (изменение цены после события). |
| `scripts/cleanup_calendar_noise.py` | Удаление мусорных записей календаря (только число без текста). |
| `scripts/cleanup_manual_duplicates.py` | Удаление записей с `source='MANUAL'`, дублирующих другую запись по (ts, ticker, content). `--dry-run` затем `--execute`. |

Модули: `services/rss_news_fetcher.py`, `services/newsapi_fetcher.py`, `services/alphavantage_fetcher.py`, `services/investing_calendar_parser.py`, `services/llm_news_fetcher.py` (запрос к LLM за новостями по тикеру).

---

## 4. Поля knowledge_base (кратко)

| Поле | Кто заполняет |
|------|----------------|
| ts, ticker, source, content, event_type, link, region, importance | Источники при сборе |
| sentiment_score, insight | Alpha Vantage (часть новостей); иначе `add_sentiment_to_news_cron.py` (LLM) |
| embedding | `sync_vector_kb_cron.py` или `VectorKB.add_event()` |
| outcome_json | `analyze_event_outcomes_cron.py` |

Подробно: [KNOWLEDGE_BASE_FIELDS.md](KNOWLEDGE_BASE_FIELDS.md).

---

## 5. Проверка

```sql
-- По источникам за последние 7 дней
SELECT source, COUNT(*) AS cnt, COUNT(DISTINCT event_type) AS types, MIN(ts), MAX(ts)
FROM knowledge_base
WHERE ts >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY source ORDER BY cnt DESC;

-- Последние записи
SELECT ts, ticker, source, event_type, LEFT(content, 60) FROM knowledge_base ORDER BY ts DESC LIMIT 20;

-- Заполненность полей
SELECT COUNT(*) AS total, COUNT(embedding) AS with_emb, COUNT(sentiment_score) AS with_sent, COUNT(outcome_json) AS with_outcome FROM knowledge_base;
```

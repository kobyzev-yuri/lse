# Новости: источники, таблица knowledge_base, скрипты

Одна таблица **knowledge_base** — все новости/события, опционально `embedding` (векторный поиск) и `outcome_json` (исход события). Источники пишут в неё через cron; backfill embedding и sentiment — отдельными скриптами.

---

## 1. Источники

| Источник | Реализовано | Работает | Ограничения |
|----------|-------------|----------|-------------|
| **RSS ЦБ** (Fed, BoE, ECB, BoJ) | Да | Да | — |
| **Investing.com Economic Calendar** | Да | Да | Страница может подгружать данные через JS |
| **Investing.com News** | Да | Да | Лента stock-market-news; тикеры из TICKERS_FAST, ключевые слова встроенные + опционально из конфига (см. ниже) |
| **NewsAPI** | Да | Да (ключ) | 100 запросов/день |
| **Alpha Vantage** (Earnings + News Sentiment) | Да | Да (ключ) | ~25 запросов/день |
| **Alpha Vantage** (Economic) | Код есть | Нет | В cron выключено; free tier часто пусто |
| **LLM (GPT/Gemini и т.д.)** | Да | Опционально | Прямой запрос к LLM: «какие новости влияют на тикер»; при `USE_LLM_NEWS=true` и настроенном `OPENAI_API_KEY` cron сохраняет ответ в knowledge_base. Список тикеров: `LLM_NEWS_TICKERS=SNDK,MU`. **LLM не ищет в интернете** — только знание из обучения; breaking news должны попадать в KB через другие источники (Investing.com News, NewsAPI и т.д.). |

**Запуск сбора:** `python scripts/fetch_news_cron.py`. В `config.env`: при необходимости `ALPHAVANTAGE_KEY`, `NEWSAPI_KEY`; для LLM-новостей — `USE_LLM_NEWS=true`, `OPENAI_API_KEY`; опционально `LLM_NEWS_TICKERS=SNDK`, `INVESTING_NEWS_TICKER_KEYWORDS` (см. ниже).

**Проверка:**  
`SELECT source, COUNT(*), MIN(ts), MAX(ts) FROM knowledge_base GROUP BY source ORDER BY 2 DESC;`

### 1.1. Investing.com News — как реализовано

- **Тикеры:** только из **TICKERS_FAST** (config.env). Других источников тикеров для этого модуля нет.
- **Ключевые слова:** для сопоставления заголовка новости с тикером используется встроенный словарь в коде (`services/investing_news_fetcher.py`, константа `BUILTIN_KEYWORDS`):

  | Тикер | Встроенные ключевые слова |
  |-------|----------------------------|
  | SNDK  | SanDisk, Western Digital, WDC, SNDK |
  | NDK   | NDK |
  | LITE  | LITE, Lumentum |
  | NBIS  | NBIS |

  Для тикера из TICKERS_FAST, которого нет в этом списке, используется одно слово — сам тикер.

- **Дополнение из конфига (необязательно):** в `config.env` можно задать переменную  
  `INVESTING_NEWS_TICKER_KEYWORDS` — она **дополняет** встроенные ключевые слова, не заменяет их. Формат: `ТИКЕР:слово1,слово2;ДРУГОЙ:слово3`.  
  Пример: `INVESTING_NEWS_TICKER_KEYWORDS=SNDK:Citron,short;LITE:opto` — для SNDK добавятся «Citron» и «short», для LITE — «opto».

- **Логика сбора:** cron вызывает `fetch_and_save_investing_news()`: загружается лента https://www.investing.com/news/stock-market-news , по заголовкам статей сопоставление с тикерами по ключевым словам, новые записи (без дубликата по `link`) пишутся в knowledge_base с `source='Investing.com News'`, `event_type='NEWS'`.

- **403 Forbidden:** сайт может отдавать 403 на запросы без браузера. В коде используются заголовки, похожие на Chrome, и предзапрос на главную страницу (cookies). Если 403 сохраняется, задайте в `config.env` прокси: `INVESTING_NEWS_PROXY=http://user:pass@host:port` или `INVESTING_NEWS_PROXY=socks5://127.0.0.1:1080`. Тогда запросы к Investing.com пойдут через прокси; остальные источники новостей не используют эту переменную.

---

## 2. FAQ

- **MANUAL** — записи, добавленные через `VectorKB.add_event()` без указания `source` (по умолчанию `'MANUAL'`). Не «ручной импорт новостей», а события из кода/бота.
- **Почему нет строки «NewsAPI» в списке source?** В БД сохраняется название издания из API (Bloomberg, Reuters, The Globe and Mail и т.д.) — это и есть новости из NewsAPI.
- **Чего не хватает:** стабильного экономического календаря (даты CPI, NFP и т.д.) и числовых макро-рядов по регионам. Для базовой работы (новости ЦБ, макро-новости, earnings) текущих источников достаточно.
- **Мусор в новостях (Alpha Vantage Earnings):** записи вида «Earnings report for TICKER» почти не несут пользы. По умолчанию они **больше не сохраняются** (в cron Alpha Vantage не пишет Earnings Calendar, если не задано `EARNINGS_CALENDAR_SAVE=true` в config.env). Уже попавшие в БД удаляются скриптом `scripts/cleanup_calendar_noise.py --execute`. Рекомендуется запускать его по cron раз в 1–7 дней (например `0 4 * * *`).

---

## 3. Скрипты

| Скрипт | Назначение |
|--------|------------|
| `scripts/fetch_news_cron.py` | Сбор из RSS, Investing.com (календарь + лента новостей), NewsAPI, Alpha Vantage; при `USE_LLM_NEWS=true` — LLM-запрос по тикерам из `LLM_NEWS_TICKERS`. |
| `scripts/add_manual_news.py` | Разовая вставка новости в knowledge_base: `python scripts/add_manual_news.py "Заголовок или текст" "https://..." [SNDK]`. Полезно для важной breaking news, которая ещё не попала в автоматический сбор. |
| `scripts/sync_vector_kb_cron.py` | Backfill `embedding` для записей с `embedding IS NULL`. |
| `scripts/add_sentiment_to_news_cron.py` | LLM: заполнение `sentiment_score` и `insight` для новостей без sentiment. |
| `scripts/analyze_event_outcomes_cron.py` | Заполнение `outcome_json` (изменение цены после события). |
| `scripts/cleanup_calendar_noise.py` | Удаление мусора: ECONOMIC_INDICATOR «только число»; **Alpha Vantage Earnings Calendar** вида «Earnings report for TICKER» (без пользы). Запуск: `python scripts/cleanup_calendar_noise.py` (dry-run), `--execute` для удаления. Рекомендуется в cron раз в 1–7 дней. |
| `scripts/cleanup_manual_duplicates.py` | Удаление записей с `source='MANUAL'`, дублирующих другую запись по (ts, ticker, content). `--dry-run` затем `--execute`. |

Модули: `services/rss_news_fetcher.py`, `services/newsapi_fetcher.py`, `services/alphavantage_fetcher.py`, `services/investing_calendar_parser.py`, `services/investing_news_fetcher.py` (лента Investing.com News, тикеры из TICKERS_FAST и встроенные ключевые слова), `services/llm_news_fetcher.py` (запрос к LLM за новостями по тикеру).

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

---

## 6. Проверка влияния новостей на тикеры (в т.ч. SNDK)

Влияние оценивается в два шага: (1) какие новости есть по тикеру и какой у них sentiment, (2) как изменилась цена после события — поле **outcome_json**.

### 6.1. Какие новости по тикеру

Все новости/события по тикеру лежат в `knowledge_base` с `ticker = 'SNDK'` (или другим). Можно смотреть список, источник, тон и исход:

```sql
-- Последние новости по SNDK за 30 дней (с sentiment и исходом, если есть)
SELECT id, ts, source, event_type,
       LEFT(content, 80) AS content_preview,
       sentiment_score,
       outcome_json->>'outcome' AS outcome,
       (outcome_json->>'price_change_pct')::float AS price_change_pct
FROM knowledge_base
WHERE ticker = 'SNDK'
  AND ts >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY ts DESC
LIMIT 50;
```

- **sentiment_score** — заполняется Alpha Vantage при сборе или позже скриптом `add_sentiment_to_news_cron.py` (LLM). Чем ниже значение, тем негативнее тон.
- **outcome_json** — заполняется скриптом **analyze_event_outcomes_cron.py** не сразу, а когда событию «исполнилось» N дней (по умолчанию 7), чтобы можно было посмотреть изменение цены после новости.

### 6.2. Как считается исход (влияние на цену)

- **Скрипт:** `scripts/analyze_event_outcomes_cron.py` (в cron обычно раз в день, например в 4:00).
- **Логика:** для записей в `knowledge_base` без `outcome_json`, у которых дата события старше N дней (переменная окружения `EVENT_OUTCOME_DAYS_AFTER`, по умолч. 7), берётся цена тикера из `quotes` на дату события и через N дней. По изменению цены в % формируется исход: `outcome` (POSITIVE / NEGATIVE / NEUTRAL), `price_change_pct`, при необходимости `sentiment_match` (совпал ли sentiment с направлением движения).
- **Запуск вручную:**  
  `EVENT_OUTCOME_DAYS_AFTER=7 python scripts/analyze_event_outcomes_cron.py`  
  (для SNDK нужны дневные котировки в `quotes` на дату события и через 7 дней.)

После выполнения у событий по SNDK появится или обновится `outcome_json` — по нему и смотрите влияние.

### 6.3. Сводка по тикеру (SQL)

```sql
-- По SNDK: сколько новостей, у скольких есть sentiment и исход
SELECT
  COUNT(*) AS total,
  COUNT(sentiment_score) AS with_sentiment,
  COUNT(outcome_json) AS with_outcome
FROM knowledge_base
WHERE ticker = 'SNDK' AND ts >= CURRENT_DATE - INTERVAL '90 days';

-- Новости с заполненным исходом — удобно для разбора
SELECT ts, source, LEFT(content, 60),
       sentiment_score,
       outcome_json->>'outcome' AS outcome,
       outcome_json->>'price_change_pct' AS change_pct
FROM knowledge_base
WHERE ticker = 'SNDK' AND outcome_json IS NOT NULL
ORDER BY ts DESC
LIMIT 20;
```

### 6.4. Скрипт для быстрой сводки

Из корня проекта:

```bash
python scripts/check_news_impact.py SNDK
```

Скрипт выводит: число новостей по тикеру за последние 90 дней, число с sentiment и с outcome, последние несколько записей с исходом (outcome, price_change_pct). Без аргумента используется тикер SNDK.

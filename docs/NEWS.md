# Новости: источники, таблица knowledge_base, скрипты

Одна таблица **knowledge_base** — все новости/события, опционально `embedding` (векторный поиск) и `outcome_json` (исход события). Источники пишут в неё через cron; backfill embedding и sentiment — отдельными скриптами.

**Целевая архитектура агрегированного новостного сигнала, горизонтов, LLM и политики входа:** [NEWS_SIGNAL_ARCHITECTURE.md](NEWS_SIGNAL_ARCHITECTURE.md).

**Проверка цепочки после cron в Telegram:** команда `/news TICKER` в LSE-боте (`services/telegram_bot.py`) читает `knowledge_base` и отдаёт ответ в стиле nyse `/news`: краткое HTML-сообщение с **draft_bias** (среднее `(sentiment_score−0.5)×2` по строкам), **news.bias** (как `AnalystAgent.calculate_weighted_sentiment`), режим **Gate** SKIP/LITE/FULL с теми же порогами, что `PROFILE_GAME5M` в nyse (пояснение — без вызова LLM, только диагностика). Полный отчёт с формулами и таблицей — HTML-файл вложением. Реализация: `services/kb_news_report.py`. Окно выборки: `KB_NEWS_LOOKBACK_HOURS` (по умолчанию 336 ч ≈ 14 дней).

**Лимиты бесплатных API и фильтрация по тикерам:** см. [docs/NEWS_LIMITS.md](NEWS_LIMITS.md). Конфиг: `KB_INGEST_TRACKED_TICKERS_ONLY` (по умолчанию сохраняем всё входящее от Alpha Vantage / LLM-новостей без отсечения по списку тикеров).

---

## Миграция новостей в LSE (расширенная `knowledge_base`, NYSE-style тикерные ленты)

Цель: привести запись в **`knowledge_base`** к одной расширенной схеме (миграция `db/knowledge_pg/sql/010_knowledge_base_nyse.sql`): `exchange`, `symbol`, `external_id`, `content_sha256`, `raw_payload`, плюс дедуп по `external_id` там, где он стабилен.

### Что уже сделано в коде

| Область | Изменение |
|---------|-----------|
| **Экономический календарь Investing.com** | `save_events_to_db` пишет расширенные поля; дедуп через `INSERT … ON CONFLICT DO NOTHING` по `external_id` (детерминированный ключ). См. `services/kb_extended_fields.py`, `services/investing_calendar_parser.py`. |
| **Тикерные новости (как в NYSE `news_merge`)** | Модуль `services/ticker_news_merge_fetcher.py`: **Yahoo** (`yfinance.get_news`) + опционально **Marketaux** при `MARKETAUX_API_KEY`; merge/dedup; вставка с `external_id` / `content_sha256` / `raw_payload`. |
| **Оркестратор** | `scripts/fetch_news_cron.py`: режимы `--mode tickers` (только TickerNews), `core` и `all` включают TickerNews; `core-fast` — по-прежнему RSS + Alpha Vantage **без** TickerNews. |
| **Ключи и конфиг** | Имена `NEWSAPI_KEY`, `ALPHAVANTAGE_KEY`, `MARKETAUX_API_KEY` согласованы с NYSE; опционально `NYSE_CONFIG_PATH` подмешивает пустые ключи из `nyse/config.env` (`config_loader.py`). Шпаргалка — в `config.env.example` (блок «News: источники…»). |
| **Диагностика** | `scripts/check_news_sources.py` показывает наличие ключа Marketaux и `NYSE_CONFIG_PATH`. |

Импорт NYSE→LSE по JSONL по-прежнему: `nyse/scripts/export_news_jsonl.py` → `scripts/import_news_jsonl_to_kb.py` (отдельный путь, не заменяет cron LSE).

### Развёртывание на сервере (осторожно, вне игровых окон)

1. **Не обязательно менять cron в тот же день**, что и релиз кода: новый функционал не ломает старые режимы, пока вы не добавите задачи.
2. **Проверка вручную:** `python scripts/fetch_news_cron.py --mode tickers` (в Docker: `docker compose exec <container> python scripts/fetch_news_cron.py --mode tickers`). Убедиться по `logs/news_fetch.log` и выборке из `knowledge_base`.
3. **Cron:** типичный безопасный шаг — **добавить** отдельную строку с `--mode tickers` и **отдельным** `flock`-локом, не заменяя сразу `core-fast`. Не подменять `core-fast` на `core` без анализа: в `core` входит NewsAPI, и он может **дублировать** уже существующую задачу `--mode newsapi` по лимитам API.
4. **Ключи:** `MARKETAUX_API_KEY` можно продублировать в `lse/config.env` на сервере или использовать `NYSE_CONFIG_PATH`.

### Дальше (отдельная итерация)

- Вынести общее ядро merge/dedup/`external_id` для репозиториев NYSE и LSE.
- Поэтапно привести остальные fetchers (NewsAPI, RSS, Investing news) к той же схеме полей, где ещё не заполнены `raw_payload` / `external_id`.

---

## 1. Источники

| Источник | Реализовано | Работает | Ограничения |
|----------|-------------|----------|-------------|
| **RSS ЦБ** (Fed, BoE, ECB, BoJ) | Да | Да | — |
| **Yahoo + Marketaux** (тикерные, merge) | Да | Да | Yahoo без ключа; Marketaux — по `MARKETAUX_API_KEY` (дополнение к Yahoo, не замена). См. `TICKER_NEWS_*`, режим cron `--mode tickers` / в `core`/`all`. |
| **Investing.com Economic Calendar** | Да | Да | Страница может подгружать данные через JS |
| **Investing.com News** | Да | Да | Лента stock-market-news; тикеры из TICKERS_FAST, ключевые слова встроенные + опционально из конфига (см. ниже) |
| **NewsAPI** | Да | Да (ключ) | ~100 запросов/день (каждая страница /everything = отдельный запрос) |
| **Alpha Vantage** (Earnings + News Sentiment) | Да | Да (ключ) | ~25 запросов/день |
| **Alpha Vantage** (Economic) | Код есть | Нет | В cron выключено; free tier часто пусто |

### 1.0. Ключи и имена (LSE и NYSE)

Чтобы не плодить расхождения с репозиторием **NYSE**, в LSE используются **те же имена переменных**, где это возможно:

| Переменная | Назначение |
|------------|------------|
| `NEWSAPI_KEY` | NewsAPI.org (и в NYSE, и в LSE) |
| `ALPHAVANTAGE_KEY` | Alpha Vantage (в коде LSE также читается `ALPHAVANTAGE_API_KEY`) |
| `MARKETAUX_API_KEY` | Marketaux (слой TickerNews в LSE; в NYSE — в merged news) |
| `NYSE_CONFIG_PATH` | Опционально: путь к `nyse/config.env` — в LSE подмешиваются **только пустые** ключи (не дублировать секреты в двух файлах) |

Полный список и тюнинг по префиксам (`NEWSAPI_*`, `INVESTING_NEWS_*`, `TICKER_NEWS_*`, …) — в **`config.env.example`** (блок «News: источники…»).

**Запуск сбора:** `python scripts/fetch_news_cron.py`. Рекомендуется в cron **каждые 15 минут** (`*/15 * * * *`), чтобы подтягивать новости за весь день с утра. В `config.env`: при необходимости `ALPHAVANTAGE_KEY`, `NEWSAPI_KEY`, `MARKETAUX_API_KEY`; опционально `INVESTING_NEWS_TICKER_KEYWORDS` (см. ниже).

**Проверка:**  
`SELECT source, COUNT(*), MIN(ts), MAX(ts) FROM knowledge_base GROUP BY source ORDER BY 2 DESC;`

### 1.1. Как обеспечить поступление новостей

1. **Cron** — запуск `fetch_news_cron.py` каждые 15 минут: `*/15 * * * * ... fetch_news_cron.py` (в `setup_cron.sh` / `setup_cron_docker.sh` уже так).
2. **Лог** — в конце каждого запуска в `logs/news_fetch.log` появляется строка **«За этот запуск всего сохранено новых записей: N»** и по каждому источнику: «RSS: сохранено X новых, дубликатов Y», «NewsAPI: сохранено Z новых» и т.д. Если всегда 0 — см. п. 3–4.
3. **Диагностика** — выполните один раз:  
   `python scripts/check_news_sources.py` (на сервере: `docker compose exec lse python scripts/check_news_sources.py`).  
   Скрипт покажет: число записей в БД, последнюю дату, результат одного прогона RSS, задан ли NEWSAPI_KEY/ALPHAVANTAGE_KEY.
4. **Типичные причины «0 новых»:** нет доступа в интернет из контейнера/хоста; NEWSAPI_KEY не задан (макро-новости только с ключом); Investing.com отдаёт 403 — задать `INVESTING_NEWS_PROXY`; RSS фиды ЦБ недоступны (блокировка/сеть). Проверьте путь к логу: при запуске из Docker лог пишется внутрь контейнера (например `project_root/logs/news_fetch.log`); если логи монтируются на хост, смотрите тот же путь на хосте.

**NewsAPI 429 (Too Many Requests):** бесплатный план ограничивает число запросов в сутки. Раньше макро-блок делал **5 отдельных запросов** × до **5 страниц** каждый — лимит исчерпывался за один прогон. Сейчас по умолчанию **один объединённый запрос** и `NEWSAPI_MAX_PAGES=1` (см. `config.env.example`). После исчерпания 429 включается **cooldown** на `NEWSAPI_COOLDOWN_AFTER_429_HOURS` часов (файл в `logs/.newsapi_cooldown_until` — удалите для сброса). Платный план NewsAPI или реже вызывать `--mode newsapi` в cron — альтернативы.

**Investing.com Read timeout:** при медленном ответе сайта увеличьте `INVESTING_NEWS_TIMEOUT` (сек); при таймауте лента запрашивается повторно один раз.

### 1.2. Investing.com News — как реализовано

- **Тикеры:** только из **TICKERS_FAST** (config.env). Других источников тикеров для этого модуля нет.

- **Фильтр «тикер не в списке»:** раньше при сопоставлении заголовка с тикером, которого нет в `get_tracked_tickers_for_kb()`, новость **отбрасывалась** (`continue`). При пустом `TICKERS_*` в конфиге подставлялся полный `BUILTIN_KEYWORDS`, матч шёл на SNDK/LITE и т.д., а в tracked оставались только `MACRO`/`US_MACRO` — вся лента могла не сохраняться. Сейчас по умолчанию такие строки **сохраняются как `MACRO`**. Отключить: `INVESTING_NEWS_STRICT_TRACKED_ONLY=true`.
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
- **Мусор в новостях (Alpha Vantage Earnings):** записи вида «Earnings report for TICKER» почти не несут пользы. По умолчанию они **больше не сохраняются** (в cron Alpha Vantage не пишет Earnings Calendar, если не задано `EARNINGS_CALENDAR_SAVE=true` в config.env). Уже попавшие в БД удаляются скриптом `scripts/cleanup_calendar_noise.py --execute`. Рекомендуется запускать его по cron раз в 1–7 дней (например `30 4 * * *`).
- **Предупреждение PostgreSQL про collation:** при обновлении ОС/glibc в логах может появляться «несовпадение версии для правила сортировки» (БД создана с 2.39, ОС даёт 2.42). На работу приложения это не влияет. Чтобы убрать предупреждение, от имени суперпользователя БД выполните: `ALTER DATABASE lse_trading REFRESH COLLATION VERSION;`

---

## 3. Скрипты

| Скрипт | Назначение |
|--------|------------|
| `scripts/fetch_news_cron.py` | Сбор из RSS, Investing.com (календарь + лента), NewsAPI, Alpha Vantage, TickerNews (Yahoo + Marketaux в режимах `core`/`all`/`tickers`). |
| `scripts/check_news_sources.py` | Диагностика: число записей в БД, один прогон RSS, наличие ключей NEWSAPI / Alpha Vantage / Marketaux. Запуск при «новостей нет». |
| `scripts/add_manual_news.py` | Разовая вставка новости в knowledge_base: `python scripts/add_manual_news.py "Заголовок или текст" "https://..." [SNDK]`. Полезно для важной breaking news, которая ещё не попала в автоматический сбор. |
| `scripts/sync_vector_kb_cron.py` | Backfill `embedding` для записей с `embedding IS NULL`. |
| `scripts/add_sentiment_to_news_cron.py` | LLM: заполнение `sentiment_score` и `insight` для новостей без sentiment. |
| `scripts/analyze_event_outcomes_cron.py` | Заполнение `outcome_json` (изменение цены после события). |
| `scripts/cleanup_calendar_noise.py` | Удаление мусора: ECONOMIC_INDICATOR «только число»; **Alpha Vantage Earnings Calendar** вида «Earnings report for TICKER» (без пользы). Запуск: `python scripts/cleanup_calendar_noise.py` (dry-run), `--execute` для удаления. Рекомендуется в cron раз в 1–7 дней. |
| `scripts/cron_watchdog.py` | Сканирует логи cron (последние 500 строк каждого) на строки с ERROR, Exception, Traceback, failed и т.п. и пишет находки в `logs/cron_watchdog.log`. При `CRON_WATCHDOG_TELEGRAM=true` или `--telegram` при находках отправляет уведомление в Telegram (TELEGRAM_SIGNAL_CHAT_IDS). В cron: каждый час в :45. |
| `scripts/cleanup_manual_duplicates.py` | Удаление записей с `source='MANUAL'`, дублирующих другую запись по (ts, ticker, content). `--dry-run` затем `--execute`. |

Модули: `services/rss_news_fetcher.py`, `services/ticker_news_merge_fetcher.py` (Yahoo + Marketaux), `services/newsapi_fetcher.py`, `services/alphavantage_fetcher.py`, `services/investing_calendar_parser.py`, `services/investing_news_fetcher.py` (лента Investing.com News, тикеры из TICKERS_FAST и встроенные ключевые слова).

---

## 4. Поля knowledge_base (кратко)

| Поле | Кто заполняет |
|------|----------------|
| ts, ticker, source, content, event_type, link, region, importance, ingested_at | Источники при сборе (`ingested_at` — обычно `NOW()` при вставке) |
| sentiment_score, insight | Alpha Vantage (часть новостей); иначе `add_sentiment_to_news_cron.py` (LLM) |
| embedding | `sync_vector_kb_cron.py` или `VectorKB.add_event()` |
| outcome_json | `analyze_event_outcomes_cron.py` |

Подробно: [KNOWLEDGE_BASE_FIELDS.md](KNOWLEDGE_BASE_FIELDS.md). Полная схема всех таблиц: [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md).

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

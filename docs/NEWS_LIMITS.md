# Лимиты внешних источников новостей (бесплатные планы)

Ориентиры на 2025–2026; уточняйте на сайтах провайдеров — правила меняются.

| Источник | Как в проекте | Типичный бесплатный лимит | Примечание |
|----------|----------------|---------------------------|------------|
| **RSS** (ФРС, BoE, ECB, BoJ) | `rss_news_fetcher.py` | Нет жёсткой квоты API | Публичные фиды; разумная частота cron (например каждые 15 мин), не DDoS. |
| **NewsAPI** (`/v2/everything`) | `newsapi_fetcher.py` | **~100 запросов/сутки** (developer) | **Каждая страница** ответа = **отдельный** HTTP-запрос. Увеличить объём: платный план или `NEWSAPI_MAX_PAGES` / реже cron. После 429 — cooldown (`NEWSAPI_COOLDOWN_AFTER_429_HOURS`). |
| **Alpha Vantage** | `alphavantage_fetcher.py` | **~25 запросов/сутки** (free) | Earnings + news sentiment быстро сжигают лимит. В конфиге по умолчанию выключены лишние серии. |
| **Investing.com News** (HTML лента) | `investing_news_fetcher.py` | Нет квоты «как у API» | Риск **429/403/таймаутов**; прокси `INVESTING_NEWS_PROXY`, паузы в коде. |
| **Investing.com Economic Calendar** (JSON API, как NYSE) | `investing_calendar_api.py` → `investing_calendar_parser.py` | Публичный эндпоинт без ключа | Пагинация, retry на **429**; без авто-fallback на HTML (legacy: `INVESTING_CALENDAR_USE_HTML=true`). |
| **KB /news gate (nyse-конвейер)** | `services/nyse_news_pipeline.py` + `kb_news_report.py` | TF-IDF кластер REG (`NYSE_REGIME_CLUSTER*`, как в nyse) | `draft_impulse` → `single_scalar_draft_bias`; гейт без отдельной ветки GEO по словарю. |
| **LLM** (генерация тем в вебе) | `llm_service.py` | Лимит вашего провайдера (ProxyAPI) | Оплачивается по токенам, не путать с лимитами NewsAPI. |

## Фильтрация по тикерам при записи в KB

- **`KB_INGEST_TRACKED_TICKERS_ONLY`** (по умолчанию **`false`**): если **`true`**, при сохранении новостей **Alpha Vantage** и **LLM-генерации** в БД учитывается только список `get_tracked_tickers_for_kb()` (TICKERS_FAST/MEDIUM/LONG + MACRO/US_MACRO). Если **`false`** — сохраняется **всё**, что вернул источник; сентимент и отбор под LLM можно делать позже (`add_sentiment_to_news_cron` и т.д.).
- **Investing.com News:** при несовпадении тикера со списком сохранение как **MACRO** (если не включён `INVESTING_NEWS_STRICT_TRACKED_ONLY`).

## Сколько «получить по максимуму»

1. **RSS** — без ключа: максимум полезной нагрузки = стабильный cron + не обрывать фиды.
2. **NewsAPI** — на free tier: **мало страниц в день**; для «всего рынка» нужен **платный** план или другой агрегатор.
3. **Alpha Vantage** — на free: **несколько** запросов в день на весь набор тикеров; расширить — платный план или убрать часть вызовов.
4. **Дубли** — отсекаются по `link`/`URL`, не по тикеру.

## Бюджет запросов NewsAPI (важно)

Один запуск `fetch_news_cron.py --mode newsapi` делает:

- **Макро:** до `NEWSAPI_MAX_PAGES` запросов (страница 1, 2, … — каждая **отдельный** HTTP-вызов).
- **Equity** (если `NEWSAPI_FETCH_EQUITY=true`): ещё до `NEWSAPI_EQUITY_MAX_PAGES` запросов.

Пример: `NEWSAPI_MAX_PAGES=2` и `NEWSAPI_EQUITY_MAX_PAGES=2` → до **4** запроса за один прогон newsapi.  
Крон `*/2` по часам = до ~48 запросов/сутки только newsapi — укладывайтесь в **~100** на developer-план или уменьшайте частоту/страницы.

**Практика «больше текста для LLM» (free):**

| Переменная | Смысл | Разумные значения |
|------------|--------|-------------------|
| `NEWSAPI_DAYS_BACK` | Глубина дат в запросе | `14` (до 30 в коде) |
| `NEWSAPI_MAX_PAGES` | Статей на макро-запрос | `2`–`3` |
| `NEWSAPI_FETCH_EQUITY` | Второй запрос по тикерам | `true` |
| `NEWSAPI_EQUITY_MAX_PAGES` | Страниц equity | `1`–`2` |
| `INVESTING_NEWS_MAX_ARTICLES` | Заголовков с ленты Investing за запуск | `40`–`80` |
| `KB_NEWS_LOOKBACK_HOURS` | Сколько часов истории KB смотрит аналитик | `336` (14 суток) |

После **429** создаётся cooldown-файл — пока он активен, NewsAPI не вызывается; удалите файл или дождитесь таймаута (см. `NEWSAPI_COOLDOWN_AFTER_429_HOURS`).

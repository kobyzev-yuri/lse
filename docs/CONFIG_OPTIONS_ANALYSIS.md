# Анализ конфига config.env: полнота опций и рекомендации

## 1. Опции, которые есть в config.env.example

| Опция | Назначение | Рекомендация |
|-------|------------|--------------|
| DATABASE_URL | PostgreSQL | Обязательно. |
| OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_TEMPERATURE, OPENAI_TIMEOUT | LLM | Обязательно для /ask, sentiment, LLM-новостей. |
| HF_MODEL_NAME | Эмбеддинги (локальная модель) | По умолчанию intfloat/multilingual-e5-base. |
| USE_OPENAI_EMBEDDINGS, USE_GEMINI_EMBEDDINGS, GEMINI_API_KEY | Эмбеддинги (облако) | Опционально, если локальная модель падает. |
| SENTIMENT_AUTO_CALCULATE, USE_LLM | Sentiment и backfill | USE_LLM=true для add_sentiment_to_news_cron. |
| INITIAL_CASH_USD, COMMISSION_RATE, STOP_LOSS_LEVEL | Торговля | Для песочницы. |
| SANDBOX_SLIPPAGE_SELL_PCT | Проскальзывание при продаже | В примере закомментировано — при необходимости раскомментировать. |
| TICKERS_FAST, TICKERS_MEDIUM, TICKERS_LONG | Группы тикеров | См. docs/TICKER_GROUPS.md. |
| GAME_5M_* | Параметры игры 5m | Все перечислены в примере. |
| PREMARKET_ALERT_TELEGRAM | Алерт премаркета в Telegram | В примере закомментировано. |
| WEB_HOST, WEB_PORT | Веб-интерфейс | Отдельно от api/bot_app (HOST/PORT). |
| NEWSAPI_KEY, ALPHAVANTAGE_KEY | Ключи новостей | Опционально. |
| ALPHAVANTAGE_FETCH_ECONOMIC, ALPHAVANTAGE_FETCH_TECHNICAL | Alpha Vantage макро/техн. | В примере закомментировано, по умолчанию false. |
| INVESTING_NEWS_TICKER_KEYWORDS, INVESTING_NEWS_PROXY | Investing.com | В примере закомментировано. |
| USE_LLM_NEWS, LLM_NEWS_TICKERS | LLM как источник новостей | В примере закомментировано. |
| TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, TELEGRAM_WEBHOOK_URL, TELEGRAM_PROXY_URL | Telegram бот | TELEGRAM_BOT_TOKEN обязателен для бота. |
| LOG_LEVEL, LOG_DIR | Логи | По желанию. |

---

## 2. Опции, используемые в коде, но отсутствующие в config.env.example

| Опция | Где используется | Дефолт в коде | Рекомендация |
|-------|-----------------|----------------|--------------|
| **EARNINGS_CALENDAR_SAVE** | alphavantage_fetcher | false | Добавить в пример (false — не сохранять earnings в KB). |
| **EARNINGS_TRACK_TICKERS** | fetch_news_cron | MSFT,SNDK,MU,LITE,ALAB,TER | Добавить в пример (тикеры для Alpha Vantage Earnings, если включён сохранение). |
| **TELEGRAM_SIGNAL_CHAT_IDS** | send_sndk_signal_cron, premarket_cron, telegram_signal | — | Добавить: чаты для рассылки сигналов (через запятую). |
| **TELEGRAM_SIGNAL_CHAT_ID** | telegram_signal | — | Один чат для сигналов (если не заданы CHAT_IDS). |
| **TELEGRAM_SIGNAL_MENTIONS** | send_sndk_signal_cron | — | Упоминания в сообщениях (например @user). |
| **TELEGRAM_DASHBOARD_CHAT_ID** | send_dashboard_cron, telegram_signal | — | Чат для дашборда. |
| **TRADING_CYCLE_TICKERS** | trading_cycle_cron, ticker_groups | — | Явный список тикеров портфельной игры (иначе MEDIUM+LONG). |
| **DASHBOARD_WATCHLIST** | dashboard_builder | SNDK,MU,LITE,ALAB,TER,MSFT | Добавить в пример: тикеры для дашборда. |
| **LLM_NEWS_COOLDOWN_HOURS** | llm_news_fetcher | 24 | Добавить в пример: не чаще одной LLM-новости по тикеру за N часов. |
| **HOST, PORT** | api/bot_app | 0.0.0.0, 8080 | Отдельный сервер API бота; добавить в пример, если используется api/bot_app. |

---

## 3. Опции только через os.environ (не config_loader)

Эти переменные читаются через `os.environ.get()`; при запуске из cron/systemd они подхватываются из config.env, если он загружен через `export $(grep -v '^#' config.env | xargs)` или аналогично.

| Опция | Где | Дефолт | Рекомендация |
|-------|-----|--------|--------------|
| ALPHAVANTAGE_TIMEOUT | alphavantage_fetcher | 90 | Опционально: таймаут запроса (сек). |
| ALPHAVANTAGE_MAX_RETRIES | alphavantage_fetcher | 3 | Опционально. |
| ALPHAVANTAGE_RETRY_DELAY | alphavantage_fetcher | 10 | Опционально. |
| ALPHAVANTAGE_MIN_DELAY_SEC | alphavantage_fetcher | 1.0 | Пауза между запросами (лимит 1 req/s). |
| ALPHAVANTAGE_DELAY_AFTER_ERROR | alphavantage_fetcher | 15 | Пауза после ошибки (сек). |
| ALPHAVANTAGE_DELAY_BETWEEN_TICKERS | alphavantage_fetcher | 15 | Между тикерами. |
| ALPHAVANTAGE_DELAY_BETWEEN_INDICATORS | alphavantage_fetcher | 13 | Между индикаторами. |
| INVESTING_CALENDAR_DEBUG_HTML | investing_calendar_parser | — | true — сохранять HTML для отладки. |

**Рекомендация:** вынести в config.env.example в секцию «Alpha Vantage (доп.)» как закомментированные строки, чтобы было понятно, что можно тонко настроить.

---

## 4. Итоговые рекомендации

1. **Добавить в config.env.example** (с комментариями):
   - EARNINGS_CALENDAR_SAVE=false
   - EARNINGS_TRACK_TICKERS (если нужен earnings)
   - TELEGRAM_SIGNAL_CHAT_IDS / TELEGRAM_SIGNAL_CHAT_ID
   - TELEGRAM_SIGNAL_MENTIONS
   - TELEGRAM_DASHBOARD_CHAT_ID
   - TRADING_CYCLE_TICKERS
   - DASHBOARD_WATCHLIST
   - LLM_NEWS_COOLDOWN_HOURS=24
   - Секцию «Alpha Vantage (доп.)» с таймаутами и задержками (закомментировано)
   - INVESTING_CALENDAR_DEBUG_HTML (закомментировано)
   - HOST, PORT для api/bot_app (если используется)

2. **Полнота:** для минимального запуска достаточно DATABASE_URL, OPENAI_API_KEY (или прокси), TELEGRAM_BOT_TOKEN, TICKERS_FAST. Остальное — по мере использования cron, дашборда, сигналов и новостей.

3. **Один источник правды:** опции Alpha Vantage сейчас частично через get_config_value (ALPHAVANTAGE_KEY, EARNINGS_CALENDAR_SAVE), частично через os.environ. Для единообразия можно читать все через config_loader (config.env подгружается при импорте, но переменные в os.environ должны быть выставлены до импорта, если скрипт не вызывает load_config сам). В текущей схеме (cron через `python script.py`, config загружается при первом get_config_value) os.environ может быть пуст для этих ключей — тогда лучше читать ALPHAVANTAGE_TIMEOUT и т.д. через get_config_value в alphavantage_fetcher.

4. **Документация:** CONFIG_SETUP.md устарел (нет упоминания EARNINGS_CALENDAR_SAVE, Telegram Signal/Dashboard, DASHBOARD_WATCHLIST). Имеет смысл обновить его ссылкой на config.env.example и на этот анализ.

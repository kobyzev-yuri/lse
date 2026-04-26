# config.env: актуальная карта опций

`config.env.example` — основной источник правды по доступным настройкам. Этот документ не дублирует каждую строку примера, а объясняет группы опций, порядок приоритета и ключевые переключатели, которые чаще всего влияют на поведение системы.

---

## 1. Порядок чтения конфига

`config_loader.get_config_value(KEY)` возвращает значение в таком порядке:

1. Переменная окружения процесса (`os.environ`) — самый высокий приоритет.
2. Объединённый конфиг из файлов:
   - основной `config.env` рядом с `config_loader.py`;
   - overlay из `NYSE_CONFIG_PATH`, если задан, только для пустых/отсутствующих ключей LSE;
   - `config.secrets.env` или путь из `LSE_CONFIG_SECRETS` / `CONFIG_SECRETS_FILE` — секреты перекрывают основной файл;
   - `config.security.env` или путь из `LSE_CONFIG_SECURITY` / `CONFIG_SECURITY_FILE` — последний file-overlay.
3. Значение по умолчанию в вызове `get_config_value(KEY, default)`.

Практический вывод: если в Docker/cron задана переменная окружения, она перекрывает строку в `config.env`. Это особенно важно для `TRADING_CYCLE_ENABLED`, `USE_LLM`, `DATABASE_URL`, ключей API и risk-профиля.

---

## 2. Секреты и обязательный минимум

Минимально для локального запуска:

| Группа | Ключи | Назначение |
|--------|-------|------------|
| БД | `DATABASE_URL` | PostgreSQL; `get_database_url()` принудительно использует базу `lse_trading` и timezone `Europe/Moscow`. |
| LLM | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` | Основной OpenAI-compatible маршрут, по умолчанию ProxyAPI. |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` | Нужны для бота; для рассылки сигналов также `TELEGRAM_SIGNAL_CHAT_IDS` или `TELEGRAM_SIGNAL_CHAT_ID`. |
| Тикеры | `TICKERS_FAST`, `TICKERS_MEDIUM`, `TICKERS_LONG` | Базовые группы инструментов для игр, новостей и отчётов. |

Секреты лучше держать в `config.secrets.env`, а не в основном `config.env`: `DATABASE_URL`, API-ключи, токены Telegram, прокси с паролями.

---

## 3. LLM и провайдеры

Основной chat-контур:

| Ключи | Смысл |
|-------|-------|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_TEMPERATURE`, `OPENAI_TIMEOUT` | Основной OpenAI-compatible клиент. Для ProxyAPI base обычно `https://api.proxyapi.ru/openai/v1`. |
| `OPENAI_TIMEOUT_PROMPT_ENTRY` | Увеличенный timeout для тяжёлых `/prompt_entry`, LLM-анализатора сделок и больших JSON-контекстов. |
| `OPENAI_CHAT_USE_MAX_COMPLETION_TOKENS` | Совместимость с моделями, где нужен `max_completion_tokens` вместо `max_tokens`. |
| `ANALYZER_LLM_MAX_COMPLETION_TOKENS` | Лимит ответа LLM в trade analyzer. |
| `ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_TIMEOUT` | Отдельный Anthropic/Claude-маршрут, если используется. |
| `LLM_COMPARE_MODELS` | Список моделей для сравнения в LLM-инструментах. |

Глобальный переключатель:

| Ключ | Смысл |
|------|-------|
| `USE_LLM` | Включает/отключает LLM в аналитическом контуре: Telegram `/signal`, `/recommend`, `/ask`, `/prompt_entry`, portfolio fusion и LLM-блоки 5m-отчётов. |

---

## 4. Embeddings и Vector KB

| Ключи | Смысл |
|-------|-------|
| `HF_MODEL_NAME` | Локальная модель sentence-transformers для vector KB. Дефолт в примере: `intfloat/multilingual-e5-base`. |
| `USE_OPENAI_EMBEDDINGS` | Включить облачные OpenAI-compatible embeddings. Используются те же `OPENAI_API_KEY` / `OPENAI_BASE_URL`. |
| `USE_GEMINI_EMBEDDINGS`, `GEMINI_API_KEY` | Альтернативные Gemini embeddings. |

Если локальная модель падает на конкретном хосте, включается облачный вариант. Подробности по использованию Vector KB — `docs/VECTOR_KB_USAGE.md`.

---

## 5. Sentiment и новости в KB

| Ключи | Смысл |
|-------|-------|
| `SENTIMENT_AUTO_CALCULATE` | Считать sentiment при импорте новостей. |
| `SENTIMENT_METHOD` | `llm` или `transformers`. LLM даёт insight, transformers дешевле и локальнее. |
| `SENTIMENT_MODEL` | Hugging Face модель для `transformers`, если выбран локальный метод. |
| `KB_NEWS_LOOKBACK_HOURS` | Окно новостей для аналитика и KB-сигнала, дефолтно 336 часов. |
| `KB_INGEST_TRACKED_TICKERS_ONLY` | Если true — сохранять в KB только tracked tickers + MACRO. |

---

## 6. Портфельная игра

Портфельная игра описана отдельно в `docs/PORTFOLIO_GAME.md`. Ключевые опции:

| Ключ | Смысл |
|------|-------|
| `TRADING_CYCLE_ENABLED` | Главный выключатель `scripts/trading_cycle_cron.py`. Если false или пусто — сделки не исполняются. |
| `TRADING_CYCLE_USE_LLM` | Включает LLM-шаг `portfolio_fusion` внутри portfolio cron. По умолчанию cron работает без HTTP LLM. |
| `TRADING_CYCLE_TICKERS` | Явный список торгуемых тикеров портфельной игры. Пусто = `TICKERS_MEDIUM + TICKERS_LONG`. |
| `PORTFOLIO_TAKE_PROFIT_PCT` | Фолбэк тейка, если стратегия не записала `take_profit`; `0` = не закрывать только по этому правилу. |
| `PORTFOLIO_STOP_LOSS_ENABLED` | Включает stop по `STOP_LOSS_LEVEL`. Может перекрываться через `strategy_parameters`. |
| `PORTFOLIO_EXIT_ONLY_TAKE` | Если true — портфель закрывается только по тейку. |
| `STOP_LOSS_LEVEL` | Порог stop как отношение цены к входу; `0.95` примерно -5%. |
| `SANDBOX_SLIPPAGE_SELL_PCT` | Консервативное проскальзывание при продаже в sandbox. |

Risk capacity берётся не из `config.env`, а из `local/risk_limits.json`, `config/risk_limits.sandbox.json` или `config/risk_limits.defaults.json` через `utils/risk_manager.py`.

---

## 7. GAME_5M

Каноническое описание алгоритма — `docs/GAME_5M_CALCULATIONS_AND_REPORTING.md`. Группы ключей:

| Группа | Ключи |
|--------|------|
| Список и частота | `GAME_5M_TICKERS`, `GAME_5M_COOLDOWN_MINUTES`, `GAME_5M_SIGNAL_CRON_MINUTES` |
| Корреляция | `GAME_5M_CORRELATION_CONTEXT`, `GAME_5M_CORRELATION_EXCLUDE_PORTFOLIO` |
| Входные правила | `GAME_5M_RSI_*`, `GAME_5M_MOMENTUM_*`, `GAME_5M_RTH_MOMENTUM_BUY_MIN`, `GAME_5M_PRICE_TO_LOW5D_MULT_MAX`, `GAME_5M_VOLATILITY_*` |
| VIX context | `GAME_5M_VIX_CONTEXT_ENABLED`, `GAME_5M_VIX_COMFORT_MAX`, `GAME_5M_VIX_STRONG_COMFORT_MAX`, `GAME_5M_VIX_RELAX_VERY_NEGATIVE_NEWS` |
| Выходы | `GAME_5M_STOP_LOSS_ENABLED`, `GAME_5M_EXIT_ONLY_TAKE`, `GAME_5M_TAKE_PROFIT_PCT`, `GAME_5M_TAKE_PROFIT_MIN_PCT`, `GAME_5M_TAKE_MOMENTUM_FACTOR`, `GAME_5M_SESSION_END_*`, `GAME_5M_MAX_POSITION_*` |
| Висяки | `GAME_5M_HANGER_TUNE_JSON`, `GAME_5M_HANGER_TUNE_APPLY_TAKE`, `GAME_5M_HANGER_DUAL_MODE`, `GAME_5M_HANGER_LIVE_*` |
| Защита от раннего разворота | `GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED`, `GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT`, `GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT` |
| Early de-risk | `GAME_5M_EARLY_DERISK_ENABLED`, `GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES`, `GAME_5M_EARLY_DERISK_MAX_LOSS_PCT`, `GAME_5M_EARLY_DERISK_MOMENTUM_BELOW` |
| CatBoost | `GAME_5M_CATBOOST_*` |
| Platform API | `PLATFORM_GAME_API_ENABLED`, `PLATFORM_GAME_API_URL`, `PLATFORM_GAME_API_TIMEOUT_SEC` |

Пер-тикерные ключи (`GAME_5M_TAKE_PROFIT_PCT_SNDK`, `GAME_5M_MAX_POSITION_DAYS_NBIS` и т.п.) читаются обычным `get_config_value()`. В веб-редактор `/parameters` автоматически добавляются только префиксы, указанные в `config_loader._GAME_5M_TICKER_KEY_PREFIXES`.

---

## 8. Premarket, watchdog и сервисные кроны

| Ключи | Смысл |
|-------|-------|
| `PREMARKET_ALERT_TELEGRAM` | Включить Telegram-алерт премаркета. |
| `PREMARKET_ENTRY_PREVIEW_5M` | Добавить прогноз входа GAME_5M в премаркет-сводку. |
| `PREMARKET_STRESS_TICKERS`, `PREMARKET_STRESS_GAP_PCT`, `PREMARKET_STRESS_ALERT_FRIDAY_ONLY` | Стресс-алерты по валютам/нефти/макро перед открытием. |
| `CRON_WATCHDOG_TELEGRAM` | Отправлять ошибки cron watchdog в Telegram. |
| `RESTART_CMD` | Команда перезапуска после правок через web `/parameters`. |

---

## 9. NewsAPI, Alpha Vantage, Marketaux, Investing

### NewsAPI

| Ключи | Смысл |
|-------|-------|
| `NEWSAPI_KEY` | API key. |
| `NEWSAPI_MACRO_SINGLE_QUERY` | Один общий macro-запрос вместо нескольких, экономит quota. |
| `NEWSAPI_MACRO_QUERY` | Пользовательский macro-запрос. |
| `NEWSAPI_MAX_PAGES`, `NEWSAPI_DAYS_BACK` | Глубина и объём macro-загрузки. |
| `NEWSAPI_FETCH_EQUITY`, `NEWSAPI_EQUITY_MAX_PAGES`, `NEWSAPI_EQUITY_TICKERS` | Вторая equity-загрузка по тикерам. |
| `NEWSAPI_COOLDOWN_AFTER_429_HOURS`, `NEWSAPI_COOLDOWN_AFTER_426_HOURS`, `NEWSAPI_COOLDOWN_FILE` | Cooldown после quota/plan ошибок. |

### Alpha Vantage

| Ключи | Смысл |
|-------|-------|
| `ALPHAVANTAGE_KEY` | Основной ключ. |
| `ALPHAVANTAGE_FETCH_ECONOMIC`, `ALPHAVANTAGE_FETCH_TECHNICAL` | Дорогие по quota блоки; по умолчанию выключать. |
| `EARNINGS_CALENDAR_SAVE` | Сохранять earnings calendar в KB; обычно false, чтобы не шуметь. |
| `EARNINGS_TRACK_TICKERS` | Тикеры для earnings. |
| `ALPHAVANTAGE_USE_SYSTEM_PROXY` | Использовать системный proxy. |

Часть тонких Alpha Vantage параметров (`ALPHAVANTAGE_TIMEOUT`, `ALPHAVANTAGE_MAX_RETRIES`, `ALPHAVANTAGE_RETRY_DELAY`, `ALPHAVANTAGE_MIN_DELAY_SEC`, `ALPHAVANTAGE_DELAY_AFTER_ERROR`, `ALPHAVANTAGE_DELAY_BETWEEN_TICKERS`, `ALPHAVANTAGE_DELAY_BETWEEN_INDICATORS`) читается через `os.environ.get()` в `services/alphavantage_fetcher.py`; если нужно менять их из cron/Docker, задавайте как environment или явно экспортируйте.

### Marketaux и ticker news

| Ключи | Смысл |
|-------|-------|
| `MARKETAUX_API_KEY` | Дополнительный источник ticker news. |
| `NYSE_CONFIG_PATH` | Подмешать ключи из соседнего NYSE-конфига, если в LSE они пустые. |
| `TICKER_NEWS_TICKERS`, `TICKER_NEWS_LOOKBACK_HOURS`, `TICKER_NEWS_MAX_PER_TICKER`, `TICKER_NEWS_EXCHANGE` | Настройки Yahoo/Marketaux ticker-news слоя. |

### Investing.com

| Ключи | Смысл |
|-------|-------|
| `INVESTING_NEWS_MAX_ARTICLES` | Сколько заголовков сканировать за запуск. |
| `INVESTING_NEWS_TICKER_KEYWORDS` | Дополнительные keyword mapping для тикеров. |
| `INVESTING_NEWS_PROXY`, `INVESTING_NEWS_TIMEOUT`, `INVESTING_NEWS_STRICT_TRACKED_ONLY` | Proxy, timeout и политика сохранения unmatched новостей. |
| `INVESTING_CALENDAR_USE_HTML`, `INVESTING_CALENDAR_USE_SYSTEM_PROXY` | Economic calendar; основной режим — JSON API, HTML только legacy. |

---

## 10. Telegram и отчёты

| Ключи | Смысл |
|-------|-------|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_WEBHOOK_URL`, `TELEGRAM_PROXY_URL` | Основная настройка бота. |
| `TELEGRAM_SIGNAL_CHAT_IDS`, `TELEGRAM_SIGNAL_CHAT_ID`, `TELEGRAM_SIGNAL_MENTIONS` | Рассылка сигналов GAME_5M, premarket и портфельных сделок. |
| `TELEGRAM_DASHBOARD_CHAT_ID` | Дашборд-чат. |
| `TELEGRAM_CLOSED_REPORT_DEFAULT`, `TELEGRAM_CLOSED_REPORT_MAX`, `WEB_CLOSED_REPORT_MAX` | Лимиты closed-отчётов в Telegram и web. |

---

## 11. Web и autotune

| Ключи | Смысл |
|-------|-------|
| `WEB_HOST`, `WEB_PORT` | Web UI. В docker-compose порт может быть проброшен иначе. |
| `ANALYZER_AUTOTUNE_APPLY` | Если 0 — autotune только предлагает кандидата и пишет состояние. |
| `ANALYZER_AUTOTUNE_MIN_TRADES` | Сколько новых сделок нужно после изменения, чтобы pending стал ready for review. |
| `ANALYZER_AUTOTUNE_DENY_KEYS` | Ключи, которые autotune не должен менять. |
| `ANALYZER_AUTOTUNE_PREFER_PREFIXES` | Приоритеты выбора кандидатов. |
| `ANALYZER_AUTOTUNE_STATE_PATH` | Путь к state-файлу. |

---

## 12. Опции, которые читаются напрямую из os.environ

Большинство новых ключей читается через `get_config_value()`, но часть всё ещё напрямую через `os.environ.get()`:

| Ключи | Где |
|-------|-----|
| `RISK_LIMITS_PROFILE`, `LSE_SANDBOX` | `utils/risk_manager.py` |
| `ALPHAVANTAGE_TIMEOUT`, `ALPHAVANTAGE_MAX_RETRIES`, `ALPHAVANTAGE_RETRY_DELAY`, `ALPHAVANTAGE_MIN_DELAY_SEC`, `ALPHAVANTAGE_DELAY_AFTER_ERROR`, `ALPHAVANTAGE_DELAY_BETWEEN_TICKERS`, `ALPHAVANTAGE_DELAY_BETWEEN_INDICATORS` | `services/alphavantage_fetcher.py` |
| `INVESTING_CALENDAR_DEBUG_HTML` | `services/investing_calendar_parser.py` |
| `NEWSAPI_COOLDOWN_FILE` | `services/newsapi_fetcher.py` |
| `OPENAI_CHAT_USE_MAX_COMPLETION_TOKENS`, `ANALYZER_LLM_MAX_COMPLETION_TOKENS` | LLM/analyzer path |
| `ANALYZER_STATE_PATH`, `ANALYZER_SNAPSHOT_DIR`, `ANALYZER_TUNE_SKIP_KEYS` | Analyzer scripts |

Если эти значения должны работать в cron или Docker, задавайте их в environment процесса. Просто строка в `config.env` сработает только для кода, который читает через `get_config_value()` или явно экспортирует файл.

---

## 13. Правила сопровождения

1. `config.env.example` должен содержать все пользовательские ключи, которые должны быть видны в web `/parameters`.
2. Секреты не добавлять в `config.env.example` реальными значениями; использовать placeholder или `config.secrets.env.example`.
3. Для новых `GAME_5M_*` ключей сразу обновлять `docs/GAME_5M_CALCULATIONS_AND_REPORTING.md`, если меняется поведение игры.
4. Для портфельных ключей обновлять `docs/PORTFOLIO_GAME.md`.
5. Если ключ читается через `os.environ.get()`, явно отмечать это в документе или перевести чтение на `get_config_value()`.

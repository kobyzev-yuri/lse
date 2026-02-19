# Итоги по новостям и данным (2026-02-19)

## Источники новостей и данных

| Источник | Статус | Описание |
|----------|--------|----------|
| **RSS** (Fed, ECB, BOJ, BOE) | ✅ | Заголовки и текст, без sentiment. Сохранение в `knowledge_base`. |
| **Investing.com Economic Calendar** | ⚠️ | Парсер готов; страница может подгружать таблицу через JS — таблица часто не находится. Макро дублируется через Alpha Vantage. |
| **Alpha Vantage** | ✅ | Earnings Calendar, News Sentiment, Economic Indicators (CPI, GDP, Fed Rate, Treasury, Unemployment), Technical Indicators (RSI, MACD, BBANDS, ADX, STOCH). Retry и таймаут 90 с. |
| **NewsAPI** | ✅ | Агрегатор новостей по тикерам/регионам. |

## Изменения в коде

- **init_db.py**: тикер золота XAUUSD=X заменён на GC=F (Yahoo); добавлены колонки в `quotes` (macd, bbands_*, adx, stoch_*), в `knowledge_base` (event_type, importance, link); проверка пустых данных при seed.
- **services/alphavantage_fetcher.py**: Economic и Technical Indicators; сохранение в БД; обработка ответа с ключом `Information`; `_get_with_retry` (таймаут 90 с, до 3 повторов, задержка 10 с).
- **services/investing_calendar_parser.py**: расширенный поиск таблицы календаря; опция `INVESTING_CALENDAR_DEBUG_HTML=1` для сохранения HTML в `/tmp`.
- **scripts/fetch_news_cron.py**: вызов `fetch_all_alphavantage_data` с флагами экономических и технических индикаторов; опции в config: `ALPHAVANTAGE_FETCH_ECONOMIC`, `ALPHAVANTAGE_FETCH_TECHNICAL`.
- **BUSINESS_PROCESSES.md**: разделы 10 (Telegram бот, webhook), 11 (деплой Cloud Run и отдельный сервер БД/КБ).
- **docs/DEPLOY_INSTRUCTIONS.md**, **docs/NEWS_AND_SENTIMENT_SUMMARY.md**: добавлены/обновлены.
- **README.md**, **scripts/trading_cycle_cron.py**: тикер золота GC=F вместо XAUUSD=X.

## Тестирование

- Полный прогон: `python scripts/fetch_news_cron.py`
- Только Alpha Vantage: `python services/alphavantage_fetcher.py`
- Инструкции: раздел «Как протестировать изменения» в `docs/NEWS_SOURCES_IMPLEMENTATION.md`

## Примечания

- При таймауте Alpha Vantage после всех повторов в коде должен быть `return None` вместо `raise` в `_get_with_retry` (см. комментарии в коде или отдельную инструкцию).
- Экономические индикаторы Alpha Vantage на бесплатном плане могут возвращать только `Information` (лимит/премиум) — тогда макро не сохраняется.
- Cron: `./setup_cron.sh` — задача новостей раз в час.

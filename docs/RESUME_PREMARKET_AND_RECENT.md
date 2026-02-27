# Резюме: премаркет и последние доработки

## Премаркет (план Алекса)

**Цель:** учитывать премаркет US при решениях «входить / не входить», «вход по текущей цене или лимитом / ждать открытия», оценка апсайда на день, явный запрет входа при риске.

### Реализовано

| Компонент | Описание |
|-----------|----------|
| **services/premarket.py** | `get_premarket_context(ticker)` — цена премаркета (yfinance prepost=True), гэп % к предыдущему закрытию, минуты до открытия 9:30 ET. |
| **market_session** | В контекст добавлено поле `minutes_until_open` при фазе PRE_MARKET. |
| **recommend_5m** | При PRE_MARKET вызывается премаркет-контекст; в выводе: `premarket_last`, `premarket_gap_pct`, `prev_close`, `minutes_until_open`; цена в ответе = цена премаркета. Рекомендация по входу: «войти сейчас» / «ждать 9:30 ET» / «лимит ниже $X». |
| **Оценка апсайда и совет по входу** | `estimated_upside_pct_day`, `suggested_take_profit_price` (цель на день для close-ордера). `entry_advice`: ALLOW / CAUTION / AVOID + `entry_advice_reason` (негатив в новостях, волатильность, премаркет гэп вниз). |
| **AnalystAgent + LLM** | При PRE_MARKET в промпт LLM добавляется блок «Контекст сессии»: премаркет, гэп %, минуты до открытия. Дневные рекомендации с LLM учитывают премаркет. |
| **scripts/premarket_cron.py** | Крон 16:30 MSK пн–пт: сбор премаркета по TICKERS_FAST и тикерам портфельной игры, лог в `logs/premarket_cron.log`. При `PREMARKET_ALERT_TELEGRAM=true` — алерт в Telegram. |
| **Отображение** | Telegram `/recommend5m` и API `GET /api/recommend5m`: выводятся апсайд на день, цель по цене, блок «Вход: CAUTION/AVOID», блок «Премаркет» с рекомендацией. |

### Документы и проверка

- **План:** [docs/PREMARKET_PLAN.md](PREMARKET_PLAN.md)
- **Сниппеты для проверки:** [docs/SNIPPETS_PREMARKET_CHECK.md](SNIPPETS_PREMARKET_CHECK.md)
- **Конфиг:** `PREMARKET_ALERT_TELEGRAM` в config.env (см. config.env.example)

---

## История сделок и уведомления

- **Команда `/history`:** поддержка фильтра по тикеру: `/history` — последние 15 сделок по всем тикерам; `/history SNDK` — только по SNDK; `/history SNDK 30` — по SNDK, лимит 30. В ответе для каждой сделки выводится стратегия: `[GAME_5M]`, `[Portfolio]`, `[Manual]`. Реализация: `execution_agent.get_trade_history(limit, ticker=None, strategy_name=None)`.
- **Уведомления в Telegram:** сигналы 5m (BUY/STRONG_BUY) шлёт `send_sndk_signal_cron.py` в чаты из `TELEGRAM_SIGNAL_CHAT_IDS`. Сделки портфельной игры (BUY/SELL по тикерам MEDIUM+LONG) после каждого запуска `trading_cycle_cron.py` тоже отправляются в те же чаты с пометкой «Портфель» и названием стратегии. Общая отправка вынесена в `services/telegram_signal.py` (get_signal_chat_ids, send_telegram_message). В crontab ничего дополнительно настраивать не нужно.

---

## Прочие последние доработки

- **Группы тикеров:** TICKERS_FAST / TICKERS_MEDIUM / TICKERS_LONG в config.env; портфельная игра использует MEDIUM+LONG (или TRADING_CYCLE_TICKERS). GAME_5m — только TICKERS_FAST. [docs/TICKER_GROUPS.md](TICKER_GROUPS.md)
- **Добавление тикера:** инструкция в TICKER_GROUPS.md (цены: `update_prices.py AMD`; новости: EARNINGS_TRACK_TICKERS, LLM_NEWS_TICKERS; Investing.com — по всем группам). [docs/TICKER_GROUPS.md](TICKER_GROUPS.md)
- **Крон 5m:** каждые 5 мин пн–пт; при закрытой бирже — ранний выход без 5m-запросов.
- **Настройки игр в config.env:** секция «Игры», GAME_5M_COOLDOWN_MINUTES, TRADING_CYCLE_TICKERS; аргументы кронов для переопределения тикеров.

---

## Бизнес-процессы (кратко)

- **Игра 5m:** cron каждые 5 мин → recommend_5m → при BUY/STRONG_BUY рассылка + запись входа; закрытие по тейку/стопу/времени (2 дня)/SELL. Премаркет: данные в решении, вход отложен до 9:30 ET.
- **Портфельная игра:** cron 9:00, 13:00, 17:00 → ExecutionAgent по тикерам MEDIUM+LONG → сигналы AnalystAgent (с учётом премаркета в LLM), стоп-лосс ~5%.
- **Премаркет-крон:** 16:30 MSK пн–пт → сбор премаркета по тикерам → лог и опционально алерт в Telegram.

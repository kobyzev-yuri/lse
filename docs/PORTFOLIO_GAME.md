# Портфельная игра: алгоритм, расчёты и отчётность

Документ фиксирует текущую логику **портфельной игры**: какие тикеры берутся в работу, как выбирается стратегия, когда открывается позиция, как считаются размер позиции, тейк и стоп, и где смотреть результат. Это отдельный контур от **GAME_5M**.

---

## 1. Что такое портфельная игра

- **Горизонт:** дневные данные `quotes`, свинг / несколько сессий, не 5-минутная интрадей-игра.
- **Направление:** сейчас исполняются только входы в long (`BUY` / `STRONG_BUY`) и закрытия уже открытых long-позиций.
- **Исполнитель:** `scripts/trading_cycle_cron.py` → `ExecutionAgent.run_for_tickers()`.
- **Таблицы:** сделки пишутся в `trade_history`, текущие позиции и кэш — в `portfolio_state`.
- **Отделение от GAME_5M:** владелец позиции определяется по `trade_history.strategy_name`. Позиции `GAME_5M` портфельный крон не закрывает; их ведёт `send_sndk_signal_cron.py`.

Портфельный cron по умолчанию **ничего не делает**, если `TRADING_CYCLE_ENABLED` не равен `true`, `1` или `yes`.

---

## 2. Запуск и тикеры

Основной запуск:

```bash
cd /path/to/lse
python scripts/trading_cycle_cron.py
```

Cron-пример из кода:

```cron
0 9,13,17 * * 1-5 cd /path/to/lse && python scripts/trading_cycle_cron.py
```

Список тикеров:

1. Если задан `TRADING_CYCLE_TICKERS`, берётся он.
2. Иначе берётся объединение `TICKERS_MEDIUM + TICKERS_LONG` без дублей.
3. Тикеры из `TICKERS_INDICATOR_ONLY` или начинающиеся с `^` используются как индикаторы для корреляции, но по ним не открываются позиции.
4. Аргумент командной строки переопределяет список торгуемых тикеров:

```bash
python scripts/trading_cycle_cron.py "MSFT,ORCL,AMD"
```

---

## 3. Источники данных

Для каждого тикера портфельный контур использует:

- `quotes`: последние дневные OHLCV, `close`, `open`, `sma_5`, `volatility_5`, `rsi`; средняя волатильность за 20 дней считается отдельно.
- `knowledge_base`: свежие новости, sentiment, insight; sentiment приводится к центрированной шкале `[-1; +1]`.
- `^VIX`: режим рынка (`vix_value`, `vix_regime`) для смягчения/ужесточения выбора стратегии.
- Корреляции: 30-дневная матрица по полному кластеру портфеля, включая индикаторы.

Если данных по тикеру недостаточно, `AnalystAgent` возвращает `NO_DATA`, и покупка не выполняется.

---

## 4. Как выбирается стратегия

Выбор делает `StrategyManager.select_strategy()` на дневных данных. Порядок важен:

| Приоритет | Стратегия | Условие выбора |
|----------:|-----------|----------------|
| 1 | `Geopolitical Bounce` | Предыдущая сессия упала минимум на 2% (`prev_day_return_pct <= -2.0`) и волатильность не ниже обычной. |
| 2 | `Volatile Gap` | Волатильность 5d выше средней 20d примерно в 1.5 раза, плюс гэп >3%, макро/много новостей или экстремальный sentiment. При VIX <20 пороги чуть жёстче. |
| 3 | `Momentum` | Спокойный рынок, цена выше `SMA_5`, волатильность ниже средней, sentiment положительный. При `LOW_FEAR` / VIX <18 окно по волатильности чуть шире. |
| 4 | `Mean Reversion` | Волатильность повышенная, sentiment нейтральный, цена заметно отклонена от `SMA_5`. |
| 5 | первая подходящая | Fallback: пройти все стратегии и взять первую с `is_suitable=True`. |
| 6 | `Neutral` | Ничего не подошло; консервативный `HOLD`. |

Сигнал стратегии (`BUY`, `STRONG_BUY`, `HOLD`, иногда `SELL`) затем может быть передан в LLM как базовая рекомендация.

---

## 5. LLM: когда участвует, а когда нет

В cron-е портфельной игры LLM-шаг **отключён по умолчанию**. Включение:

```env
TRADING_CYCLE_USE_LLM=true
```

Если LLM выключен, исполняется только базовый контур: техника → новости → `StrategyManager` → сигнал.

Если LLM включён, вызывается `AnalystAgent.get_decision_with_llm()` с профилем `portfolio_fusion`. В prompt передаются:

- технические данные тикера;
- выбранная стратегия и её базовый сигнал;
- новости и KB-сигнал;
- VIX / режим рынка;
- 30-дневные корреляции текущего тикера с кластером;
- сигналы по тикерам, которые уже были обработаны в этом же запуске;
- статистика закрытых сделок по стратегиям за последние 180 дней, если доступна.

Итоговое решение: если LLM вернул валидный `decision_fused` / `decision` из `BUY`, `STRONG_BUY`, `HOLD`, берётся оно; иначе остаётся базовый сигнал стратегии.

Telegram-команды `/signal`, `/recommend`, `/prompt_entry portfolio` используют тот же аналитический путь, но форма вывода другая: краткая рекомендация или полный отчёт/промпт.

---

## 6. Кластерный контекст

Кластер строится один раз на запуск:

1. `trading_cycle_cron.py` берёт полный список портфеля (`TRADING_CYCLE_TICKERS` или `TICKERS_MEDIUM + TICKERS_LONG`).
2. Из него формируется список торгуемых тикеров: индикаторы (`^VIX` и т.п.) исключаются из торговли.
3. Для полного списка считается корреляционная матрица за 30 дней.
4. По каждому торгуемому тикеру решение принимается отдельно, но в контекст подставляется строка корреляций именно этого тикера с остальными.
5. По мере прохода по списку накапливается `other_signals`: следующий тикер видит сигналы по уже обработанным тикерам.

При BUY в `trade_history.context_json.cluster` сохраняется снимок:

| Поле | Смысл |
|------|-------|
| `tickers` | Полный кластер запуска. |
| `correlation_this_ticker` | Корреляции текущего тикера с остальными. |
| `other_signals_at_decision` | Сигналы, уже полученные в этом запуске до решения по текущему тикеру. |

Это позволяет потом проверять, помог ли кластерный контекст в реальных сделках.

---

## 7. Когда открывается позиция

Покупка выполняется только если итоговое решение `BUY` или `STRONG_BUY`.

Перед записью BUY проверяется:

1. По тикеру ещё нет открытой позиции по `trade_history` / `portfolio_state`.
2. Есть свежая цена в `quotes`.
3. Сейчас торговые часы NYSE по `RiskManager.is_trading_hours()`.
4. Есть свободный `CASH`.
5. Расчётное количество после `floor(allocation / price)` больше нуля.
6. Размер позиции и общая экспозиция проходят `RiskManager`.

Если любая проверка не проходит, сигнал логируется, но сделка в БД не записывается.

---

## 8. Размер позиции

Формула в `ExecutionAgent._execute_buy()`:

```text
allocation_percent = min(10%, max_single_ticker_exposure_percent)
allocation = min(CASH × allocation_percent, max_position_size_usd)
quantity = floor(allocation / current_price)
notional = quantity × current_price
total_cost = notional + commission
```

Комиссия берётся из `COMMISSION_RATE` / динамического параметра; сейчас дефолт кода — `0.0`.

Риск-лимиты загружаются в таком порядке:

1. `local/risk_limits.json`;
2. если `RISK_LIMITS_PROFILE=sandbox` или `LSE_SANDBOX=1` — `config/risk_limits.sandbox.json`;
3. иначе `config/risk_limits.defaults.json`;
4. встроенные консервативные значения, если файл не найден.

В sandbox-профиле сейчас задан капитал `$5,000,000`, максимум позиции `$2,000,000`, максимум экспозиции портфеля `99.5%`, максимум на тикер `80%`, минимум позиции `$100`.

---

## 9. Что записывается при BUY

В `portfolio_state`:

- уменьшается `CASH`;
- добавляется или увеличивается позиция по тикеру;
- пересчитывается средняя цена входа.

В `trade_history`:

- `side='BUY'`;
- `signal_type` = `BUY` или `STRONG_BUY`;
- `strategy_name` = имя выбранной стратегии (`Momentum`, `Mean Reversion`, `Volatile Gap`, `Geopolitical Bounce`, `Neutral`) или `Portfolio`, если имя пустое;
- `take_profit` и `stop_loss` из стратегии, если стратегия их вернула;
- `context_json` с техническими данными, sentiment, base decision и кластером.

### Откуда берётся `take_profit` при входе

`take_profit` — это процент доходности от цены входа, а не цена. Его считает выбранная стратегия в `calculate_signal()`:

```text
StrategyManager.select_strategy(...)
→ selected_strategy.calculate_signal(...)
→ strategy_result["take_profit"]
→ ExecutionAgent._execute_buy(..., take_profit=...)
→ trade_history.take_profit
```

Эти числа теперь вынесены в `config.env` и видны в веб-разделе `/parameters`. Внутри стратегии значение берётся через `BaseStrategy.get_parameters(default_params, target_identifier=f"TICKER:{ticker}")`:

1. стартуют кодовые дефолты стратегии;
2. поверх них накладываются ключи `PORTFOLIO_<STRATEGY>_STOP_LOSS_PCT` / `PORTFOLIO_<STRATEGY>_TAKE_PROFIT_PCT` из `config.env`;
3. поверх config накладываются параметры из `strategy_parameters` для конкретной стратегии и тикера (`TICKER:<ticker>`), если они есть.

Ключи config сейчас такие:

| Стратегия | config key stop | config key take |
|-----------|-----------------|-----------------|
| `Momentum` | `PORTFOLIO_MOMENTUM_STOP_LOSS_PCT=3` | `PORTFOLIO_MOMENTUM_TAKE_PROFIT_PCT=8` |
| `Mean Reversion` | `PORTFOLIO_MEAN_REVERSION_STOP_LOSS_PCT=5` | `PORTFOLIO_MEAN_REVERSION_TAKE_PROFIT_PCT=4` |
| `Volatile Gap` | `PORTFOLIO_VOLATILE_GAP_STOP_LOSS_PCT=7` | `PORTFOLIO_VOLATILE_GAP_TAKE_PROFIT_PCT=12` |
| `Geopolitical Bounce` | `PORTFOLIO_GEOPOLITICAL_BOUNCE_STOP_LOSS_PCT=5` | `PORTFOLIO_GEOPOLITICAL_BOUNCE_TAKE_PROFIT_PCT=4` |
| `Neutral` | `PORTFOLIO_NEUTRAL_STOP_LOSS_PCT=` | `PORTFOLIO_NEUTRAL_TAKE_PROFIT_PCT=` |

Кодовые дефолты с теми же числами остаются только как fallback, если ключа нет в config. `PORTFOLIO_TAKE_PROFIT_PCT` — другой fallback: он используется уже при закрытии, если в BUY не был сохранён стратегический `take_profit`.

### Как менять через веб

После деплоя эти ключи появляются в `/parameters`, потому что веб-редактор строит список из незакомментированных строк `config.env.example`. Сохранение в вебе пишет значение в реальный `config.env`.

Применение:

1. После изменения кода (`strategies/base_strategy.py`) нужен обычный деплой с пересборкой Docker-образа.
2. После изменения только значений в `/parameters`:
   - `trading_cycle_cron.py` подхватит новые значения на следующем запуске, потому что стартует новым процессом;
   - для долгоживущего web/API процесса нажмите restart в `/parameters` или выполните `docker compose restart lse`, если хотите сразу применять новые значения в ручных API-запусках.
3. Если для той же стратегии и `TICKER:<ticker>` есть запись в `strategy_parameters`, она перекроет значение из `config.env`.

Важно: в текущей реализации `take_profit` попадает в BUY только когда `ExecutionAgent` получил полный `strategy_result`, то есть в пути `get_decision_with_llm()` (`TRADING_CYCLE_USE_LLM=true`) или если аналитический метод вернул dict с `strategy_result`. В дефолтном cron-режиме без LLM (`TRADING_CYCLE_USE_LLM` пустой/false) `get_decision()` возвращает только строку `BUY` / `HOLD`, поэтому `trade_history.take_profit` для нового BUY может быть `NULL`.

Если в BUY `take_profit` оказался `NULL`, закрытие использует fallback `PORTFOLIO_TAKE_PROFIT_PCT` из config. Если и он равен `0` или пустой, тейк по портфельной позиции не проверяется.

---

## 10. Как закрываются позиции

После прохода по всем тикерам `ExecutionAgent.run_for_tickers()` вызывает:

```python
check_stop_losses(exclude_strategy_names=frozenset({"GAME_5M"}))
```

То есть закрываются только портфельные позиции, не `GAME_5M`.

Позиции берутся в первую очередь из `trade_history` через расчёт открытых BUY/SELL; `portfolio_state` используется как fallback для совместимости.

Порядок проверки:

1. **Стоп-лосс**, если `PORTFOLIO_STOP_LOSS_ENABLED=true` и `PORTFOLIO_EXIT_ONLY_TAKE=false`.
2. **Тейк-профит**, если задан в последнем BUY или через `PORTFOLIO_TAKE_PROFIT_PCT`.
3. Если ни стоп, ни тейк не сработали — позиция остаётся открытой.

---

## 11. Стоп-лосс

Стоп считается через лог-доходность:

```text
log_ret = ln(current_price / entry_price)
stop_threshold = ln(STOP_LOSS_LEVEL)
```

При дефолтном `STOP_LOSS_LEVEL=0.95` порог примерно соответствует падению на 5% от входа.

Условия:

- если `PORTFOLIO_STOP_LOSS_ENABLED=false`, стоп не закрывает позицию;
- если `PORTFOLIO_EXIT_ONLY_TAKE=true`, автозакрытие только по тейку, стоп не применяется;
- параметры `PORTFOLIO_STOP_LOSS_ENABLED` и `STOP_LOSS_LEVEL` могут переопределяться через `strategy_parameters` для `GLOBAL`.

---

## 12. Тейк-профит

При закрытии порог тейка берётся так:

1. `take_profit` из последнего BUY по тикеру (`trade_history.take_profit`), если он был сохранён при входе;
2. иначе `PORTFOLIO_TAKE_PROFIT_PCT` из config;
3. если порог отсутствует или `0`, тейк не проверяется, в лог пишется подсказка задать `PORTFOLIO_TAKE_PROFIT_PCT`.

Формула:

```text
pnl_pct = (current_price - entry_price) / entry_price × 100
закрыть, если pnl_pct >= take_profit
```

Пример: если `Momentum` открыл BUY по 100 и в сделку записан `take_profit=8.0`, то автозакрытие сработает при `quotes.close >= 108` (до учёта slippage/комиссии). Если при входе `take_profit=NULL`, но в config задан `PORTFOLIO_TAKE_PROFIT_PCT=3`, то закрытие сработает при `pnl_pct >= 3%`.

---

## 13. Цена продажи и отчётность

При SELL:

- цена берётся из последнего `quotes.close`;
- если задан `SANDBOX_SLIPPAGE_SELL_PCT`, цена исполнения консервативно уменьшается на этот процент;
- считается `log_ret`, `MFE` и `MAE` по `quotes.high/low` с момента входа;
- позиция удаляется/уменьшается в `portfolio_state`;
- `CASH` увеличивается на выручку минус комиссия;
- в `trade_history` пишется `side='SELL'`, причина закрытия и стратегия.

Telegram-уведомления о портфельных BUY/SELL отправляет `trading_cycle_cron.py` через `services/telegram_signal.py` в `TELEGRAM_SIGNAL_CHAT_IDS` / `TELEGRAM_SIGNAL_CHAT_ID`.

---

## 14. Где смотреть результат

| Что нужно проверить | Где смотреть |
|---------------------|--------------|
| Запуск портфельного крона | `logs/trading_cycle.log` |
| Почему BUY не записался | лог `ExecutionAgent`: `already_open`, `outside_hours`, `risk_position_size`, `risk_exposure`, `qty_zero`, и т.д. |
| Открытые позиции | `portfolio_state`, `/pending`, веб-сводка |
| История сделок | `trade_history`, `/history`, отчёты закрытых позиций |
| Кластер и prompt | `/prompt_entry portfolio` или `trade_history.context_json` у BUY |
| Веб-карточки портфеля | `/portfolio/cards`, `/portfolio/daily` |

---

## 15. Главные отличия от GAME_5M

| Пункт | Портфельная игра | GAME_5M |
|-------|------------------|---------|
| Данные | дневные `quotes` | 5m-бары + текущие quotes |
| Горизонт | дни / свинг | интрадей |
| Запуск | `trading_cycle_cron.py` | `send_sndk_signal_cron.py` |
| Тикеры | `TRADING_CYCLE_TICKERS` или MEDIUM+LONG | `GAME_5M_TICKERS` или FAST |
| Вход | `AnalystAgent` + `StrategyManager`, опц. LLM | правила 5m / `recommend_5m` |
| Выход | `ExecutionAgent.check_stop_losses()` | `services/game_5m.should_close_position()` |
| Владелец позиции | `strategy_name != GAME_5M` | `strategy_name = GAME_5M` |

Если один тикер есть в обеих играх, это нормально: закрывает позицию тот контур, который её открыл.

---

## 16. Ключевые файлы

- `scripts/trading_cycle_cron.py` — входная точка портфельного cron.
- `execution_agent.py` — исполнение BUY/SELL, risk checks, тейк/стоп.
- `analyst_agent.py` — техника, новости, sentiment, LLM fusion.
- `strategy_manager.py` — выбор стратегии.
- `strategies/*.py` — конкретные стратегии и их stop/take.
- `services/ticker_groups.py` — тикеры портфельной игры и индикаторы.
- `services/cluster_recommend.py` — корреляционная матрица.
- `utils/risk_manager.py` — лимиты капитала, позиции, экспозиции, торговые часы.
- `docs/CRONS_AND_TAKE_STOP.md` — совместная карта 5m/портфеля по крону и тейк/стоп.

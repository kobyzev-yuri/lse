# GAME_5M: алгоритм, динамический тейк и борьба с висяками

Документ фиксирует текущую реализацию **GAME_5M**: как устроена 5-минутная игра, как считается тейк, почему `TAKE_PROFIT` может не совпадать с пиком на графике, и как работает механизм борьбы с **висяками**. Старые раздельные документы перенесены в архив:

- `docs/archive/GAME_5M_CALCULATIONS_AND_REPORTING_2026-04-26.md`
- `docs/archive/GAME_5M_DYNAMIC_TAKE_EXIT_SEMANTICS_2026-04-26.md`

---

## 1. Как выглядит игра сейчас

- **Стратегия:** одна симулируемая стратегия `GAME_5M`, только long.
- **Тикеры:** `GAME_5M_TICKERS`, если задан; иначе `TICKERS_FAST`.
- **Крон:** `scripts/send_sndk_signal_cron.py`, обычно каждые 5 минут.
- **Вход:** при `BUY` / `STRONG_BUY` пишется BUY в `trade_history` со `strategy_name='GAME_5M'`.
- **Выход:** при открытой позиции крон сначала проверяет закрытие, и только если позиция не закрыта в этом же запуске, может рассматривать новый вход.
- **Хранилище:** сделки — `trade_history`; текущая открытая позиция для закрытия определяется как нетто GAME_5M, согласованное с `/pending`.

В коде входная точка выглядит так:

```python
# scripts/send_sndk_signal_cron.py
open_pos = resolve_open_position_for_game5m_close(ticker)
has_pos = open_pos is not None
...
should_close, exit_type, exit_detail = should_close_position(...)
```

---

## 2. Основной цикл по тикеру

Для каждого тикера cron делает последовательность:

1. Получает 5m-решение `get_decision_5m(ticker)` или готовое кластерное решение.
2. Собирает карточку и close-контекст:
   - текущая цена 5m;
   - `momentum_2h_pct`;
   - `bar_high`, `bar_low`;
   - `exit_bar_close`;
   - `recent_bars_high_max`, `session_high` и их timestamp.
3. Ищет открытую позицию GAME_5M.
4. Если позиция есть, проверяет закрытие через `should_close_position()`.
5. Если закрытие сработало, пишет SELL через `close_position()` и сбрасывает cooldown по тикеру.
6. Если позиции нет, а входной сигнал `BUY` / `STRONG_BUY`, пишет BUY через `record_entry()` и отправляет Telegram-сигнал.

Важно: для выхода используется **чистое техническое решение** `technical_decision_core`, а для входа — `technical_decision_effective`, куда могут быть добавлены fusion/ML-фильтры. Это сделано, чтобы дополнительные входные фильтры не ломали закрытие уже открытой позиции.

---

## 3. Параметры текущего игрового блока

Ключевые параметры берутся из `config.env` / `config.env.lse` через `config_loader.get_config_value()`. Если один ключ встречается в файле несколько раз, активным будет последнее прочитанное значение.

На текущем блоке `config.env.lse` видны такие настройки GAME_5M:

| Параметр | Значение / смысл |
|----------|------------------|
| `GAME_5M_STOP_LOSS_ENABLED=false` | Стоп по убытку выключен; основные выходы — тейк, `TIME_EXIT`, `TIME_EXIT_EARLY` если включён. |
| `GAME_5M_EXIT_ONLY_TAKE=false` | Разрешены не только тейки, но и time exits. |
| `GAME_5M_TAKE_PROFIT_MIN_PCT=3.0` | Импульс 2h ниже 3% не используется для формулы тейка; берётся cap. |
| `GAME_5M_TAKE_MOMENTUM_FACTOR=1.1` | Тейк от импульса = `momentum_2h_pct × 1.1`, но не выше cap. |
| `GAME_5M_TAKE_PROFIT_PCT=5.8` | Общий cap тейка. |
| `GAME_5M_TAKE_PROFIT_PCT_SNDK=5`, `NBIS=5.5`, `ASML=4`, `MU=0.5`, `LITE=0.5`, `CIEN=5.5` | Пер-тикерные cap; поздняя строка в конфиге перекрывает раннюю. |
| `GAME_5M_MAX_POSITION_DAYS=1` + per ticker | Грубый лимит удержания в днях. |
| `GAME_5M_MAX_POSITION_MINUTES_*` | Более точный лимит удержания в минутах; если задан, имеет приоритет над днями. |
| `GAME_5M_SESSION_END_EXIT_MINUTES=30`, `GAME_5M_SESSION_END_MIN_PROFIT_PCT=0.3` | В последние 30 минут regular session можно выйти с минимальным профитом 0.3%, если сигнал не `STRONG_BUY`. |
| `GAME_5M_HANGER_DUAL_MODE=true` | Включена live-диагностика висяков: сниженный cap применяется только если позиция классифицирована как висяк. |
| `GAME_5M_HANGER_TUNE_JSON=/app/logs/hanger_tune_open_agg.json` | JSON с предложенными cap для висяков. |
| `GAME_5M_HANGER_TUNE_APPLY_TAKE=true` | Разрешает применять cap из hanger JSON при соответствующем режиме. |

---

## 4. Как считается динамический тейк

Есть два разных числа:

- `take_pct` — целевой порог доходности в процентах от входа;
- `exit_price` — цена, которая записывается в `trade_history.price` при SELL.

Формула `take_pct`:

```python
# services/game_5m.py::_effective_take_profit_pct
cap = _take_profit_cap_pct(ticker, apply_hanger_json=apply_hanger_json)
min_take = GAME_5M_TAKE_PROFIT_MIN_PCT
momentum_factor = clamp(GAME_5M_TAKE_MOMENTUM_FACTOR, 0.3, 2.0)

if momentum_2h_pct is not None and momentum_2h_pct >= min_take:
    take_pct = min(momentum_2h_pct * momentum_factor, cap)
else:
    take_pct = cap
```

То есть импульс **не закрывает позицию сам по себе**. Он только помогает вычислить целевой порог. Закрытие происходит, когда **цена** достигла порога.

Пример с текущими параметрами:

| Тикер | Cap | Импульс 2h | Factor | `take_pct` |
|-------|----:|-----------:|-------:|-----------:|
| `SNDK` | 5.0% | 3.5% | 1.1 | `min(3.85%, 5.0%) = 3.85%` |
| `NBIS` | 5.5% | 7.0% | 1.1 | `min(7.7%, 5.5%) = 5.5%` |
| `MU` | 0.5% | 4.0% | 1.1 | `min(4.4%, 0.5%) = 0.5%` |
| `CIEN` | 5.5% | 2.0% | 1.1 | импульс ниже 3%, значит `5.5%` |

---

## 5. Когда срабатывает TAKE_PROFIT

Проверка в `should_close_position()`:

```python
price_for_take = max(current_price, bar_high) if bar_high is not None and bar_high > 0 else current_price
pnl_take_pct = (price_for_take - entry_price) / entry_price * 100.0
take_threshold = take_pct - 0.05

if pnl_take_pct >= take_threshold:
    exit_sig = "TAKE_PROFIT_SUSPEND" if apply_hanger_json is True else "TAKE_PROFIT"
    return True, exit_sig, ""
```

Ключевые детали:

- для long берётся `max(current_price, bar_high)`, поэтому короткий spike high внутри окна может триггерить тейк;
- допуск `0.05 п.п.` нужен, чтобы тейк не промахивался из-за округления;
- если сработал обычный алгоритм, в БД пишется `TAKE_PROFIT`;
- если сработал алгоритм висяка, в БД пишется `TAKE_PROFIT_SUSPEND`.

---

## 6. Откуда берётся `bar_high`

`bar_high` собирается в `services/recommend_5m.py::build_5m_close_context()` и передаётся в cron. Это не обязательно high последней свечи.

Приоритет:

1. `recent_bars_high_max` — максимум High по последним 5m-барам окна;
2. `last_bar_high`, если recent high недоступен;
3. `session_high` может поднять `bar_high`, если максимум сессии выше recent window.

Практический смысл: если цена уже касалась уровня тейка, но текущий close ниже, крон всё равно видит достижение уровня через high-агрегат.

---

## 7. Почему TAKE_PROFIT не совпадает с ценой пика

Решение о тейке принимается по `price_for_take = max(current_price, bar_high)`, но цена записи в БД при тейке берётся иначе:

```python
# scripts/send_sndk_signal_cron.py
base_exit = close_ctx.get("exit_bar_close") or price_for_check

if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
    if base_exit and base_exit > 0:
        exit_price = base_exit
    elif bar_high is not None and bar_high > 0:
        exit_price = bar_high
```

Итого:

- **триггер** может быть по high;
- **запись в БД** обычно по `exit_bar_close`;
- на графике и в отчёте SELL-маркер лежит на линии close, а не на spike high;
- `missed_upside_pct` в анализаторе может быть высоким даже при корректном тейке, потому что анализатор сравнивает realised close с лучшим high внутри сделки.

Пример чтения лога:

```text
TAKE_PROFIT ... bar_high=903.0 ... exit_bar_close=882.1 -> exit_price=882.1
```

Это означает: уровень тейка был достигнут по high, но сделка записана по close бара решения.

---

## 8. Защита от нереальной цены выхода

`close_position()` дополнительно ограничивает записываемую цену:

```python
# services/game_5m.py::close_position
# bar_high/bar_low — опционально: при TAKE_PROFIT / TAKE_PROFIT_SUSPEND цена не выше bar_high,
# при STOP_LOSS не ниже bar_low.
```

Для тейка:

- если передан `bar_high`, `exit_price` не должен быть выше него;
- если `bar_high` не передан, цена ограничивается разумным верхним cap относительно входа;
- при подозрительном отклонении от входа пишется warning.

Это защищает БД и график от ситуации, когда из `quotes` или битого 5m-бара пришла явно невозможная цена.

---

## 9. Выходы кроме TAKE_PROFIT

Проверка закрытия идёт в таком порядке:

1. `TAKE_PROFIT` / `TAKE_PROFIT_SUSPEND`.
2. Мягкий тейк у high в начале regular session.
3. `STOP_LOSS`, если `GAME_5M_STOP_LOSS_ENABLED=true`.
4. `TIME_EXIT` перед концом сессии при минимальном профите.
5. `TIME_EXIT` по лимиту минут или дней удержания.
6. `TIME_EXIT_EARLY`, если включён early de-risk.

`SELL` из текущей 5m-рекомендации **не закрывает уже открытую позицию**. В коде это зафиксировано явно:

```python
# services/game_5m.py::should_close_position
# SELL используем только как рекомендацию (в момент входа), но НЕ как причину выхода
# для уже открытой позиции.
if current_decision == "SELL":
    return False, "", ""
```

---

## 10. TIME_EXIT в конце сессии

Перед закрытием regular session действует правило:

```python
exit_min = GAME_5M_SESSION_END_EXIT_MINUTES      # например 30
min_profit = GAME_5M_SESSION_END_MIN_PROFIT_PCT # например 0.3

if minutes_until_close <= exit_min and current_decision != "STRONG_BUY":
    if pnl_current_pct >= min_profit:
        return True, "TIME_EXIT", "session_end"
```

Смысл: не переносить небольшую прибыль через ночь, если до закрытия рынка осталось мало времени. Исключение — `STRONG_BUY`: сильный сигнал разрешает удержание.

---

## 11. TIME_EXIT по сроку удержания

После ценовых проверок считается возраст позиции:

```python
max_min = _max_position_minutes(ticker)
if max_min is not None and age > timedelta(minutes=max_min):
    return True, "TIME_EXIT", "max_hold_minutes"

if age > timedelta(days=_max_position_days(ticker)):
    return True, "TIME_EXIT", "max_hold_days"
```

Если задан `GAME_5M_MAX_POSITION_MINUTES_<TICKER>` или общий `GAME_5M_MAX_POSITION_MINUTES`, лимит минут имеет приоритет над лимитом дней. Это основной механизм против бесконечного удержания позиции.

---

## 12. Early de-risk

`TIME_EXIT_EARLY` выключен по умолчанию и включается только через:

```env
GAME_5M_EARLY_DERISK_ENABLED=true
```

Условие закрытия:

```python
age >= GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES
pnl_current_pct <= GAME_5M_EARLY_DERISK_MAX_LOSS_PCT
momentum_2h_pct is None or momentum_2h_pct <= GAME_5M_EARLY_DERISK_MOMENTUM_BELOW
current_decision in ("HOLD", "SELL")
```

Смысл: если позиция уже достаточно долго в просадке, импульс не поддерживает удержание, а текущая карточка не даёт BUY, позицию можно закрыть раньше обычного срока. В текущем `config.env.lse` этот режим выключен.

---

## 13. Что такое «висяк»

**Висяк** в текущей реализации — это открытая позиция GAME_5M, которая в заданном календарном окне после входа не закрывается штатным реплеем правил по 5m-барам и при этом рынок не показывает нормального продолжения вверх.

Формальное определение в `services/game5m_param_hypothesis_backtest.py::diagnose_hanger()`:

```python
dfw = _slice_df_through_calendar_days(df5, entry_ts_et=entry_ts_et, calendar_days=hanger_calendar_days)
ex = replay_game5m_on_bars(dfw, entry_ts_et=entry_ts_et, entry_price=entry_price, ...)

if ex is not None and ex.signal_type == "TAKE_PROFIT":
    return None
if ex is not None:
    return None

sag_ok = skip_sag_check or _sagging_market(dfw, entry_price=entry_price, epsilon_log=sag_epsilon_log)
if not sag_ok:
    return None

return {"kind": "hanger", ...}
```

То есть позиция считается висяком, если:

1. есть достаточно 5m-баров в окне (`len(dfw) >= 3`);
2. реплей текущих правил в окне не дал `TAKE_PROFIT`;
3. реплей также не дал другой ранний выход (`TIME_EXIT`, `STOP_LOSS` и т.п.) — такие случаи не классифицируются как «висяк на тейке»;
4. выполняется проверка «провисания» по close, если она не отключена.

Для live-режима используется агрегатная позиция: VWAP по открытым BUY, а окно считается от последнего открывающего BUY; бары загружаются с запасом от первого BUY.

---

## 14. Как подбирается лекарство от висяка

Офлайн-подбор делает `scripts/backtest_game5m_param_hypotheses.py`.

Для открытых позиций рекомендуется агрегированный режим:

```bash
docker compose exec lse python scripts/backtest_game5m_param_hypotheses.py --mode open_agg \
  --json-out /app/logs/hanger_tune_open_agg.json
```

Логика подбора:

1. Найти открытые BUY GAME_5M.
2. Для каждого тикера собрать 5m-бары после входа.
3. Классифицировать висяк через `diagnose_hanger()`.
4. Если это висяк, подобрать более низкий cap тейка через `sweep_hanger_take_cap()`.
5. Записать результат в JSON в поле `hanger_hypotheses[].remediation_take_cap.proposed_cap_pct`.

Сетка cap строится как множители к текущему потолку:

```python
# services/game5m_param_hypothesis_backtest.py
floor_m = max(0.55, 1.0 - 0.45 * min(1.0, aggression))
np.linspace(1.0, floor_m, n)
```

Для каждого кандидата реплей проверяет: появился ли `TAKE_PROFIT` внутри окна. В JSON попадает cap, который даёт выход, но остаётся максимально близким к исходному cap среди успешных кандидатов.

---

## 15. Как лекарство применяется в live

В cron-е включён dual mode:

```env
GAME_5M_HANGER_DUAL_MODE=true
GAME_5M_HANGER_TUNE_JSON=/app/logs/hanger_tune_open_agg.json
GAME_5M_HANGER_TUNE_APPLY_TAKE=true
GAME_5M_HANGER_LIVE_CALENDAR_DAYS=6
GAME_5M_HANGER_LIVE_SAG_EPSILON_LOG=0
GAME_5M_HANGER_LIVE_SKIP_SAG=false
```

Алгоритм live:

1. Для каждой открытой позиции GAME_5M cron запускает `live_aggregate_hanger_diagnosis()`.
2. Если функция вернула `None`, позиция идёт по обычным правилам.
3. Если функция вернула `{"kind": "hanger"}`, для этой позиции выставляется `apply_hanger_json=True`.
4. `_take_profit_cap_pct(..., apply_hanger_json=True)` читает `GAME_5M_HANGER_TUNE_JSON` и берёт минимальный `proposed_cap_pct` по тикеру.
5. Эффективный cap становится `min(base_cap, proposed_cap_pct)`.
6. Формула `take_pct` остаётся той же, но с уменьшенным cap.
7. Если цена достигает нового порога, SELL пишется с `signal_type='TAKE_PROFIT_SUSPEND'`.

Кодовое ядро:

```python
# scripts/send_sndk_signal_cron.py
hdiag = live_aggregate_hanger_diagnosis(...)
apply_hanger_json = hdiag is not None

should_close, exit_type, exit_detail = should_close_position(
    open_pos,
    decision_exit,
    price_for_check,
    momentum_2h_pct=momentum_2h_pct,
    bar_high=bar_high,
    bar_low=bar_low,
    apply_hanger_json=apply_hanger_json,
)
```

```python
# services/game_5m.py
if pnl_take_pct >= take_threshold:
    exit_sig = "TAKE_PROFIT_SUSPEND" if apply_hanger_json is True else "TAKE_PROFIT"
    return True, exit_sig, ""
```

---

## 16. Почему dual mode важен

Dual mode не снижает cap всем подряд. Он применяет hanger JSON только если live-диагностика прямо сейчас классифицировала позицию как висяк.

Это видно по логу:

```text
[5m] HANGER_TACTIC SNDK: apply_hanger_json=True live_hanger_kind=hanger cap_pct=... eff_take_pct=... should_close=True exit_type=TAKE_PROFIT_SUSPEND
```

Если `apply_hanger_json=False`, значит позиция не считается висяком и идёт по обычному cap.

---

## 17. Пример: обычный тейк

Условия:

- `entry = 100.00`;
- тикер `SNDK`, cap `5.0%`;
- `momentum_2h_pct = 3.5%`;
- `factor = 1.1`.

Расчёт:

```text
take_pct = min(3.5 × 1.1, 5.0) = 3.85%
take_threshold = 3.85 - 0.05 = 3.80%
take_level ≈ 103.85
```

Если `bar_high = 103.90`, то `pnl_take_pct = 3.90%`, условие выполнено. В БД может быть записан `exit_bar_close = 103.20`, а `signal_type = TAKE_PROFIT`.

---

## 18. Пример: тейк висяка

Условия:

- `entry = 100.00`;
- обычный cap тикера `5.0%`;
- в `/app/logs/hanger_tune_open_agg.json` для тикера есть `proposed_cap_pct = 2.8`;
- live-диагностика вернула `kind='hanger'`;
- `momentum_2h_pct = 3.5%`, `factor = 1.1`.

Расчёт:

```text
base_cap = 5.0%
hanger_cap = 2.8%
cap_for_this_position = min(5.0, 2.8) = 2.8%
take_pct = min(3.5 × 1.1, 2.8) = 2.8%
take_threshold = 2.75%
```

Если текущая цена или `bar_high` даёт PnL `2.8%`, позиция закрывается как:

```text
signal_type = TAKE_PROFIT_SUSPEND
```

Это не обычный тейк стратегии, а выход по облегчённому порогу для подвисшей позиции.

---

## 19. Где смотреть диагностику

| Что проверить | Где смотреть |
|---------------|--------------|
| Почему позиция закрылась | `logs/cron_sndk_signal.log`, строки `[5m] <TICKER> закрытие: тип=...` |
| Применился ли режим висяка | `grep HANGER_TACTIC logs/cron_sndk_signal.log` |
| Какой cap взят из JSON | `cap_pct`, `eff_take_pct`, `apply_hanger_json` в строке `HANGER_TACTIC` |
| Что записано в БД | `trade_history.signal_type`, `trade_history.context_json` |
| Открытые позиции | `/pending` и `report_generator.compute_open_positions()` |
| Закрытые сделки | `/closed`, отчёт анализатора, `trade_history` |
| JSON с гипотезами | `/app/logs/hanger_tune_open_agg.json` |

---

## 20. Сводка по типам выхода

| `signal_type` | Что означает |
|---------------|--------------|
| `TAKE_PROFIT` | Цена достигла обычного динамического тейка. |
| `TAKE_PROFIT_SUSPEND` | Цена достигла сниженного тейка для позиции, классифицированной как висяк. |
| `TIME_EXIT` | Закрытие по концу сессии с минимальным профитом или по лимиту времени удержания. |
| `TIME_EXIT_EARLY` | Досрочное снижение риска из просадки; работает только если включён `GAME_5M_EARLY_DERISK_ENABLED`. |
| `STOP_LOSS` | Цена достигла стопа; сейчас не применяется, если `GAME_5M_STOP_LOSS_ENABLED=false`. |

---

## 21. Источники истины в коде

- `scripts/send_sndk_signal_cron.py` — основной цикл, dual mode, запись цены выхода, лог `HANGER_TACTIC`.
- `services/game_5m.py` — `_effective_take_profit_pct`, `_take_profit_cap_pct`, `should_close_position`, `close_position`.
- `services/recommend_5m.py` — `build_5m_close_context`, `recent_bars_high_max`, `session_high`, тексты объяснений для закрытия.
- `services/game5m_param_hypothesis_backtest.py` — `diagnose_hanger`, `live_aggregate_hanger_diagnosis`, `sweep_hanger_take_cap`.
- `scripts/backtest_game5m_param_hypotheses.py` — запуск подбора JSON для висяков.
- `services/trade_effectiveness_analyzer.py` — post factum анализ `missed_upside`, PnL и эффективности закрытий.

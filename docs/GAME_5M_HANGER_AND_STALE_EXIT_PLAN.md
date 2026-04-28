# GAME_5M: доработка hanger и stale exits

## Проблема

В текущей GAME_5M смешаны две разные проблемы, которые в отчётах выглядят похожими:

1. **Hanger / висяк**: позиция слишком долго ждёт take-profit и блокирует капитал.
2. **Stale reversal**: у позиции уже нет позитивного сетапа, текущие сигналы слабые или обратные, а цена продолжает падать вместо восстановления.

Текущий механизм `hanger` не является прогнозной моделью. Это поздний классификатор:

- реплеим текущие правила на календарном окне;
- если позиция не закрылась по реплею;
- и рынок в конце окна не выше цены входа;
- классифицируем позицию как `hanger`.

Текущее “лечение” только снижает потолок take-profit для классифицированных висяков. Это может помочь позициям около безубытка или в небольшом плюсе, но не решает глубокие отрицательные развороты вроде NBIS или ASML.

## Текущие слабые места

- `GAME_5M_EXIT_ONLY_TAKE=true` после проверки take-profit отключает `TIME_EXIT`, `TIME_EXIT_EARLY`, `STOP_LOSS` и выходы по SELL-сигналу. Поэтому per-ticker лимиты удержания в минутах не защищают капитал, пока включён этот режим.
- `hanger` определяется слишком поздно: live-окно по умолчанию равно 6 календарным дням.
- `hanger` меняет только take-profit cap. Он не закрывает плохие позиции.
- Текущий SELL-сигнал явно не используется как триггер закрытия уже открытого long.
- Нет скоринга риска зависания за первые 30/60 минут после входа.

## Целевое поведение

Логику выхода нужно разделить на три независимых слоя:

1. **Обычная фиксация прибыли**
   - Использовать существующий динамический take-profit.
   - Сохранить soft take около intraday high.

2. **Спасение recoverable hanger**
   - Сохранить идею `TAKE_PROFIT_SUSPEND`.
   - Использовать её только для позиций, которые не сломаны глубоко и ещё могут реалистично выйти в уменьшенный положительный take.

3. **Stale/reversal exit**
   - Закрывать позиции, которые превысили ожидаемый срок удержания и больше не поддерживаются текущими сигналами.
   - Это не stop-loss, а правило инвалидации по времени и сигналу.
   - На первом этапе закрывать как `TIME_EXIT_EARLY`, чтобы переиспользовать существующую отчётность. Позже можно ввести отдельный `STALE_REVERSAL_EXIT`.

## Фаза 1: немедленный контроль риска

Добавить stale/reversal rule в `services/game_5m.py::should_close_position`.

Условие:

```text
enabled = GAME_5M_STALE_REVERSAL_EXIT_ENABLED
age >= GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES
pnl_current_pct <= GAME_5M_STALE_REVERSAL_MAX_PNL_PCT
current_decision in HOLD/SELL
momentum_2h_pct <= GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW
```

Начальные значения для production tuning:

```env
GAME_5M_STALE_REVERSAL_EXIT_ENABLED=true
GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES=390
GAME_5M_STALE_REVERSAL_MAX_PNL_PCT=-1.5
GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW=0.0
```

Для тикеров повышенного риска использовать более строгие per-ticker сроки удержания:

```env
GAME_5M_MAX_POSITION_MINUTES_ASML=390
GAME_5M_MAX_POSITION_MINUTES_NBIS=780
```

Замечание по реализации: stale/reversal rule нужно проверять до широкого guard `GAME_5M_EXIT_ONLY_TAKE`. Тогда включённое правило сможет защищать от stale risk даже при legacy take-only режиме. Операционно `GAME_5M_EXIT_ONLY_TAKE=false` всё равно чище, потому что возвращает и обычный `TIME_EXIT`.

## Фаза 2: Hanger Definition v2

Заменить бинарное определение hanger на скоринговую диагностику:

```text
hanger_score = age_score
             + distance_to_take_score
             + weak_momentum_score
             + drawdown_score
             + missed_opportunity_score
```

Классы:

- `recoverable_hanger`: небольшая просадка или небольшой плюс, тренд слабый, но ещё не сломан;
- `stale_reversal`: отрицательный PnL, HOLD/SELL, слабый momentum, возраст больше ожидаемого срока удержания;
- `normal_hold`: позиция ещё молодая или поддерживается STRONG_BUY/положительным momentum.

Сниженный take cap должен получать только `recoverable_hanger`. `stale_reversal` должен закрываться.

## Фаза 3: модель stuck-risk на первых 30/60 минутах

Перед нейронной сетью стоит обучить простую supervised-модель.

Целевые метки:

- `stuck`: нет `TAKE_PROFIT` за N баров/дней или выход в минус после max hold;
- `quick_win`: `TAKE_PROFIT` в тот же день или в пределах настроенного max hold;
- `bad_reversal`: просадка выше порога и нет восстановления.

Кандидаты в признаки:

- entry RSI, `momentum_2h`, `volatility_5m`, ATR, `volume_vs_avg`;
- доходность за первые 30/60 минут после входа;
- MFE/MAE за первые 30/60 минут;
- drift текущего решения: BUY -> HOLD/SELL;
- расстояние до динамического take;
- market/session phase;
- историческая ticker-specific доля быстрых успешных входов.

Начать лучше с CatBoost или logistic regression и только потом оценивать нейронную сеть. Данные табличные и ограниченные, поэтому CatBoost, вероятно, будет лучшей первой моделью.

## Проверка

Перед production-раскаткой использовать replay:

1. Реплейнуть открытые и недавно закрытые GAME_5M сделки с новым stale/reversal rule.
2. Проверить предотвращённые убытки на кейсах типа NBIS/ASML.
3. Проверить false exits, где позиция после раннего выхода всё же восстановилась бы до take-profit.
4. Сравнить:
   - realized PnL;
   - capital-days locked;
   - missed upside after early exit;
   - количество `TIME_EXIT_EARLY`.

## Раскатка

1. Добавить code path за config flags.
2. При необходимости включить сначала в paper/log-only режиме.
3. Сначала включить для high-risk тикеров.
4. Через одну торговую неделю проверить closed reports.
5. Сделать режим дефолтным, если он снижает блокировку капитала и крупные stale losses без чрезмерного срезания быстрых восстановлений.

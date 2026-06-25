# GAME_5M Decision Architecture

**Статус:** каноническое описание алгоритма принятия решений GAME_5M.  
**Цель:** одна концепция для правил, observable baseline и ML-контуров, чтобы по мере готовности ML можно было переключать влияние через readiness/gate, а не переписывать продуктовую логику.

---

## 1. Куда идём

GAME_5M должен принимать решение через единый `decision_stack`:

1. `rules_5m` создаёт базовый технический кандидат (`BUY`, `STRONG_BUY`, `HOLD`, `SELL`).
2. Production-контуры с понятной семантикой добавляют ограничения и поддержку:
   - сессия;
   - KB/news sentiment;
   - `entry_advice`;
   - macro risk;
   - observable `premarket_gap_baseline`.
3. ML-контуры сначала работают как telemetry/caution:
   - `gap_forecast`;
   - `forecast_layer`;
   - `catboost_entry_5m`;
   - `multiday_lr`;
   - будущие recovery/event/cluster контуры.
4. Когда ML стабильно лучше baseline, он получает readiness `production` и gate mode `apply`.
5. Итоговое поле для торгового контура: `decision_effective` (при `RESOLVE=false` на legacy это `technical_decision_effective`).

Принцип: **наблюдаемая рыночная величина может быть production baseline; ML становится production только после доказанного преимущества над baseline.**

**Dual-track:** готовые контуры включаются на **legacy hot path** сразу (`*_ENABLED`, gate `apply`), параллельно пишется `decision_snapshot`. `RESOLVE=true` не обязателен для первого включения ML — см. [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md), дорожная карта единой точки — [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) §11.

---

## 2. Главная схема

```text
Market data + KB + session + premarket context
        |
        v
compute_5m_features()
        |
        v
rules_5m technical core
        |
        v
entry_advice / macro / premarket_gap_baseline
        |
        v
ML contours: forecast_layer, gap_forecast, CatBoost, multiday
        |
        v
decision_snapshot contributions
        |
        v
resolve_game5m_technical()
        |
        v
decision_effective -> cron entry/hold/exit
```

Вне regular session вход не пишется. В PRE_MARKET контуры готовят контекст, но сам вход откладывается до RTH.

---

## 3. Источник базового решения

`get_decision_5m()` загружает 5m OHLC и один раз считает признаки:

- `price`;
- `rsi_5m`;
- `volatility_5m_pct`;
- `momentum_2h_pct` с фактическим окном `momentum_window_min`;
- `momentum_rth_today_pct`;
- `session_high`;
- `pullback_from_high_pct`;
- последние high/low бары;
- `atr_5m_pct`;
- `volume_vs_avg_pct`.

Техническое ядро:

- RSI low + допустимый momentum -> `BUY` / `STRONG_BUY`;
- RTH momentum + не перекупленность -> `BUY`;
- early RTH может использовать premarket intraday momentum;
- RSI high -> `SELL`;
- высокая volatility/ATR/низкий volume могут удержать вход.

После этого KB-новости могут ослабить или запретить вход. Сессия может принудительно отложить вход.

### 3.1 Intraday Regime Router (с 2026-06-25)

**Код:** `services/game5m_intraday_regime.py`. Классифицирует **текущую** ленту по observable-фичам (`momentum_rth_today_pct`, `session_move_from_open_pct`, `pullback_from_high_pct`, `bars_since_session_high`) — без ML.

| Режим | Критерий (упрощённо) | Вход | Выход / EOD |
|-------|----------------------|------|-------------|
| `impulse_up` | RTH ≥ 2.5% или сессия ≥ 3% со свежим high | без доп. блока | `TAKE_MOMENTUM_FACTOR × 1.15` |
| `chop` | RTH < 1.5% и 2ч < 0.5% | `buy_rth_momentum` → HOLD если RTH < 1.5% | take cap ×0.85, soft-take 2% в REGULAR, EOD-flat при −0.35% |
| `fade_extended` | сессия у хая, импульс затух | все BUY → HOLD | — |
| `neutral` | остальное | базовые правила | базовые правила |

**Порядок на входе:** `decide_game5m_technical` → stale-chase guard → **intraday regime guard** → KB/VIX/…

**На выходе:** `_effective_take_profit_pct(..., d5_context=)` и `should_close_position` читают `intraday_regime` из текущего `d5` (режим пересчитывается каждый бар).

**Конфиг:** `GAME_5M_INTRADAY_REGIME_*` в `config.env`; bundle `intraday_regime_v1` в `game5m_tuning_bundles.py`. Gate: `GAME_5M_INTRADAY_REGIME_GATE_MODE=apply` (песочница — apply-first, без warm-up log_only).

**Телеметрия в сделке:** `context_json.intraday_regime`, `intraday_regime_entry_guard_*`; decision_stack contour `intraday_regime`.

---

## 4. Premarket Gap Baseline

`premarket_gap_pct` считается как:

```text
premarket_gap_pct = (premarket_last / prev_close - 1) * 100
```

Где:

- `prev_close` = последний regular close из `quotes`, fallback через Yahoo daily;
- `premarket_last` = последняя 1m цена Yahoo `prepost=True` строго до 09:30 ET;
- время и минуты до открытия считаются в ET.

### Текущая продуктовая роль

`premarket_gap_baseline` является **observable production contour**, потому что это не обученная оценка, а фактическое рыночное наблюдение до открытия.

Дефолтная логика:

- `gap <= -2%`: bearish gap, `CAUTION` / `downgrade`.
- `+1% <= gap < +4%`: bullish baseline, может поддержать вход (`boost`), если нет fade-risk.
- `gap >= +4%`: strong gap-up, `TAKE-watch` / prepare-for-open; **не downgrade сам по себе**.
- `gap >= +4%` + fade-risk: `strong_gap_up_fade_risk`, downgrade допустим.

Fade-risk контекст:

- very negative KB/news;
- macro `DOWN` или `AVOID`;
- bearish multiday horizon.

Смысл `gap >= +4%`: не “покупать любой ценой” и не “держаться в стороне”, а **готовиться к TAKE / open-play / подтверждённому входу**. Для уже открытой позиции это сигнал смотреть на фиксацию; для нового входа это сигнал ждать подтверждения 5m свечой, pullback или volume.

### Config

```env
GAME_5M_PREMARKET_GAP_BASELINE_BULLISH_MIN_PCT=1.0
GAME_5M_PREMARKET_GAP_BASELINE_NEGATIVE_CAUTION_PCT=-2.0
GAME_5M_PREMARKET_GAP_BASELINE_CHASE_RISK_PCT=4.0
GAME_5M_PREMARKET_GAP_BASELINE_BEARISH_MULTIDAY_PCT=-0.15
DECISION_STACK_PREMARKET_GAP_BASELINE_GATE_MODE=apply
```

Название `CHASE_RISK_PCT` историческое: сейчас это порог `strong_gap_up`, а не автоматический bearish downgrade.

---

## 5. ML Gap Forecast

**Сухой остаток:** гэп на **RTH open того же торгового дня** прогнозируется **до 9:30 ET**, в фазе **PRE_MARKET**, с учётом **текущего** premarket gap и макро. Это **не** прогноз open «завтра» и **не** overnight без премаркета. После open факт пишется в лог; ML error = `open_gap_pct − pred`.

### Вопрос модели

«Какой будет **open gap %** (open vs prev close) **сегодня** в 9:30 ET?»

### Обучение (история в `game5m_gap_forecast_daily`)

Одна строка = `(symbol, trade_date)`:

| | Поле | Когда |
|---|---|---|
| **X** | `premarket_gap_pct`, `pred_sector_gap_pct`, premarket-фичи, dummy тикера | снимок **до** open **того же** `trade_date` |
| **y** | `open_gap_pct` | **факт** на open **того же** `trade_date` |

Модель учится: «утром PM был X, сектор Y → open оказался Z» — **внутри одного дня**, не T→T+1.

### Predict (сегодня, в PRE_MARKET)

На вход подаётся **сегодняшний** live premarket gap + **сегодняшний** sector/macro pred. Без PM pooled ridge не строит feature vector. Fallback OLS v2: макро-гэпы сегодня + blend с PM, если \|PM\| ≥ порога.

Веб-таблица «Премаркет 1m» в PRE_MARKET пересчитывает прогноз при каждом обновлении (Yahoo live). Cron только пишет снимок в БД для Telegram и накопления истории.

### Три колонки в UI (вкладка premarket)

| Колонка | Смысл |
|---|---|
| **Baseline→open** | наивно: open ≈ текущий premarket gap |
| **ML open** | pooled ridge v2 (PM + macro → open) |
| **Effective→open** | policy `auto`: baseline, пока ML MAE ≥ baseline MAE на rolling метриках |

Те же три колонки: веб `/visualization?tab=premarket`, Telegram `/premarket` и cron `premarket_cron` (блок «base / ML / eff»).

### Effective vs вход в сделку (важно)

**Сейчас по факту ориентир на naive (PM gap).** Пока ML не обгоняет naive на rolling MAE, **Effective = Baseline = `premarket_gap_pct`**.

| Слой | Роль в продукте | Влияет на BUY/HOLD? |
|------|-----------------|---------------------|
| **Baseline→open** | наивный прогноз open % | нет (только отображение) |
| **ML open** | ridge v2, advisory | нет (`gap_forecast` → telemetry) |
| **Effective→open** | `pick_effective_open_gap_pct` (`GAME_5M_OPEN_GAP_FORECAST_POLICY=auto`) | **нет** — поле для «рабочего прогноза open» |
| **`premarket_gap_baseline`** | пороги по **PM gap** + макро/multiday → caution/boost | **да** — production в legacy + decision_stack |

**Почему Effective не подключён к входу отдельно:** вход уже использует **тот же PM gap** через `premarket_gap_baseline` (правила, не одно число). Подключать Effective сейчас = дубль без выигрыша. Когда policy `auto` переключится на ML, **прогноз open** (веб, Telegram, `forecast_layer` / `forecast_open_gap_pct`) сменится **сам**; для **входа** нужен отдельный promotion: readiness `production`, `DECISION_STACK_FORECAST_GATE_MODE=apply` (и при необходимости калибровка порогов под ML, не под PM).

`forecast_layer` (`services/game5m_forecast_layer.py`) берёт **тот же** effective через `pick_effective_open_gap_pct` → `forecast_open_gap_pct` на карточке 5m. Gate по умолчанию: `DECISION_STACK_FORECAST_GATE_MODE=log_only`, readiness `caution`.

### Источники ML

- pooled ridge v2 (`GAME_5M_PREMARKET_GAP_POOLED_ENABLED`);
- ticker OLS v2 (fallback);
- sector proxy;
- blend с premarket gap (OLS path).

Текущий статус: **monitoring/caution**, не основной источник входа. На ~90d OOS naive PM→open (MAE ≈1.36 pp) **лучше** ridge (≈1.62 pp). Production observable baseline: `premarket_gap_baseline`.

### Cron (роль)

| Скрипт | Зачем |
|---|---|
| `premarket_cron.py` | Telegram, `record_premarket_gap_snapshots()` → БД |
| `ingest_game5m_gap_forecast.py --phase open` | факт open + ML error после 9:30 |

Cron **не** обновляет live-прогноз в веб-таблице в PRE_MARKET.

### Условие будущего переключения

ML может заменить или превзойти baseline в `decision_stack`, если на живой истории:

- наблюдений достаточно: минимум 60-100 завершённых rows по GAME_5M;
- ML MAE стабильно ниже `premarket_gap_baseline` на rolling window;
- median AE и `within 1 pp` не хуже baseline;
- direction accuracy не хуже baseline;
- bias близок к нулю;
- нет деградации по отдельным ключевым тикерам.

После этого:

```env
DECISION_STACK_READINESS_GAP_FORECAST=production
DECISION_STACK_FORECAST_GATE_MODE=apply
```

Прогноз open (Effective / `forecast_open_gap_pct`) переключается policy `auto` **без смены кода**, когда rolling ML MAE < baseline MAE. Вход в сделку — **отдельный** шаг (readiness + gate apply + при необходимости `DECISION_STACK_RESOLVE_ENABLED=true`).

Важно: переключение влияния на **торговлю** — только через readiness/gate, не ручным переписыванием правил.

---

## 6. Decision Stack Contributions

Каждый контур отдаёт contribution:

- `contour_id`;
- `role`;
- `readiness`: `telemetry`, `caution`, `production`;
- `strength`: -1..+1;
- `action`: `signal`, `telemetry`, `boost`, `downgrade`, `veto`;
- `metrics`.

Ключевые контуры GAME_5M:

- `rules_5m`: production core signal.
- `session`: production veto вне RTH.
- `entry_advice`: production policy gate.
- `macro_risk`: production policy gate.
- `premarket_gap_baseline`: production observable baseline.
- `forecast_layer`: caution, объединяет ML/premarket/multiday envelope.
- `gap_forecast`: caution, ML open-gap prediction.
- `catboost_entry_5m`: caution/apply по readiness.
- `multiday_lr`: caution/apply по readiness.
- `intraday_regime`: production policy gate (chop/fade downgrade входа; telemetry regime label).
- `news_fusion`: caution/log-only до готовности.

В `resolve_game5m_technical()` применяются только contribution с gate mode `apply`. ML-контуры дополнительно должны быть `production`, иначе они остаются в snapshot как telemetry/caution.

---

## 7. Gate Modes

Общий контракт gate mode:

- `none`: контур не влияет и почти не участвует.
- `log_only`: контур пишет telemetry/snapshot, но не меняет decision.
- `apply`: контур может менять `decision_effective`.

Ключи:

```env
DECISION_STACK_ENABLED=true
DECISION_STACK_RESOLVE_ENABLED=false|true
DECISION_STACK_ENTRY_ADVICE_GATE_MODE=log_only|apply
DECISION_STACK_MACRO_GATE_MODE=log_only|apply
DECISION_STACK_PREMARKET_GAP_BASELINE_GATE_MODE=apply
DECISION_STACK_FORECAST_GATE_MODE=log_only|apply
DECISION_STACK_CATBOOST_GATE_MODE=none|log_only|apply
DECISION_STACK_MULTIDAY_GATE_MODE=none|log_only|apply
DECISION_STACK_NEWS_FUSION_GATE_MODE=none|log_only|apply
```

`DECISION_STACK_RESOLVE_ENABLED=false` означает mirror mode: фактический `decision_effective` следует legacy effective, но `projected_effective_if_resolve` показывает, что было бы при resolve. Для безопасного rollout сначала смотрим divergence, потом включаем resolve.

---

## 8. Exit Gates And Take Caps

Входной `decision_effective` и выход из открытой GAME_5M позиции разделены. Закрытие позиции делает `should_close_position()` в `services/game_5m.py`; ML-effective entry signal не должен ломать технический exit core.

Ключевые exit-контуры:

- базовый тейк по 2h momentum: `min(momentum_2h_pct × GAME_5M_TAKE_MOMENTUM_FACTOR, GAME_5M_TAKE_PROFIT_PCT[_TICKER])`;
- hanger JSON может сузить cap, если включён `GAME_5M_HANGER_TUNE_APPLY_TAKE`;
- `GAME_5M_HANGER_CAP_OVERRIDE_MARGIN_PCT` защищает от устаревшего hanger cap: если live PnL уже сильно выше суженного cap, закрытие проверяется по базовому cap;
- continuation gate может отложить `TAKE_PROFIT`, если позиция остаётся сильной; для сильного momentum допускается масштабировать trailing pullback через `GAME_5M_CONTINUATION_TRAIL_MOMENTUM_SCALE_*`.

Analyzer-контроль для этой части: `summary.by_exit_signal`, `top_profitable_missed_upside`, `game5m_hanger_v2_review`, `continuation_gate_review.continuation_gate_blocked_count` и `avg_missed_upside_blocked_by_trail`.

---

## 9. Как это влияет на реальные решения

### Пример A: умеренный bullish premarket

Контекст:

- `premarket_gap_pct = +1.8%`;
- macro UP/neutral;
- нет негативных новостей;
- technical core = `BUY`.

Результат:

- `premarket_gap_baseline.signal = bullish_gap`;
- contribution может дать `boost`;
- `decision_effective` остаётся `BUY`;
- reasoning/entry advice показывает, что observable gap поддерживает вход.

### Пример B: сильный gap-up без fade-risk

Контекст:

- `premarket_gap_pct = +4.6%`;
- macro neutral/UP;
- нет негативных новостей;
- technical core = `BUY`.

Результат:

- `premarket_gap_baseline.signal = strong_gap_up`;
- `action = telemetry`;
- `should_take_watch = true`;
- BUY не гасится автоматически;
- продуктовый смысл: подготовиться к TAKE/open-play, вход только по подтверждению.

### Пример C: сильный gap-up с fade-risk

Контекст:

- `premarket_gap_pct = +4.6%`;
- macro DOWN или bearish multiday;
- technical core = `BUY`.

Результат:

- `signal = strong_gap_up_fade_risk`;
- `action = downgrade`;
- `decision_effective` может стать `HOLD`;
- смысл: высокий риск fade после гэпа.

### Пример D: отрицательный premarket

Контекст:

- `premarket_gap_pct = -2.4%`;
- technical core = `BUY`.

Результат:

- `signal = bearish_gap`;
- `action = downgrade`;
- `entry_advice = CAUTION`;
- вход откладывается или требует лимита/подтверждения.

---

## 10. Open / Near-Open Behavior

В PRE_MARKET вход не открывается. После 09:30 ET:

- первые 5-15 минут считаются рискованными;
- `near_open_guard` защищает от широкой первой свечи;
- сильный gap-up не означает “догонять market order”;
- предпочтительный сценарий: подтверждение 5m свечой, volume, pullback, или фиксация TAKE для существующей позиции.

Связанные параметры:

```env
GAME_5M_NEAR_OPEN_BUY_GUARD=true
GAME_5M_NEAR_OPEN_WIDE_BAR_PCT=1.0
GAME_5M_NEAR_OPEN_FIRST_WINDOW_MIN=15
GAME_5M_NEAR_OPEN_STRONG_BUY_ON_WIDE_BAR=hold|buy|none
GAME_5M_NEAR_OPEN_BUY_ON_WIDE_BAR=none|hold
```

---

## 11. Что сохраняется

В `d5` и `context_json` должны быть видны:

- raw inputs: price, RSI, volatility, momentum, session phase;
- premarket fields: `premarket_last`, `premarket_gap_pct`, `minutes_until_open`;
- baseline fields:
  - `premarket_gap_baseline_signal`;
  - `premarket_gap_baseline_action`;
  - `premarket_gap_baseline_reason`;
  - `premarket_gap_take_watch`;
- ML fields:
  - `ticker_open_gap_predicted_pct`;
  - `ticker_open_gap_predicted_source`;
  - `forecast_layer`;
  - `forecast_open_gap_pct`;
  - confidence/uncertainty when available;
- `decision_snapshot` with all contributions.

Это позволяет после сделки сравнивать: что говорил observable baseline, что говорил ML, и кто реально повлиял на `decision_effective`.

---

## 12. Правило развития

Новые контуры не должны напрямую переписывать `decision` в разных местах. Правильный путь:

1. Добавить поле/модель в `d5`.
2. Добавить contribution в `decision_stack`.
3. Сначала `telemetry` или `log_only`.
4. Накопить аналитику по live DB.
5. Поднять readiness/gate mode до `apply`.
6. Проверить divergence и реальные сделки.

Так мы сохраняем единый алгоритм и можем менять влияние контуров конфигом.

---

## 13. Связанные документы

- [DECISION_STACK_ROLLOUT_PLAN.md](DECISION_STACK_ROLLOUT_PLAN.md) — rollout mechanics.
- [GAME_5M_PREMARKET_AND_IMPULSE.md](GAME_5M_PREMARKET_AND_IMPULSE.md) — детали premаркет-цены и импульса.
- [GAME_5M_CALCULATIONS_AND_REPORTING.md](GAME_5M_CALCULATIONS_AND_REPORTING.md) — расчёты входа/выхода и отчётность.
- [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md) — multiday ML contour.
- [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md) — CatBoost contour.

# GAME_5m: макро VIX / Forex / нефть

**Обновлено:** 2026-05-15

Единые правила премаркетного **risk-off** и подсказок по гэпу для карточек 5m, Telegram и (опционально) премаркет-алертов. Связанные документы: [GAME_5M_PRODUCT_IDEAS_ARBITER.md](GAME_5M_PRODUCT_IDEAS_ARBITER.md), [GAME_5M_WEB_CARDS.md](GAME_5M_WEB_CARDS.md), [GAME_5M_DEAL_PARAMS_JSON.md](GAME_5M_DEAL_PARAMS_JSON.md).

---

## Назначение

| Компонент | Файл | Роль |
|-----------|------|------|
| Расчёт макро | `services/macro_premarket_risk.py` | Гэпы VIX, Forex, нефти → `risk_level`, `equity_gap_bias`, `reasons` |
| Вход в решение 5m | `services/recommend_5m.py` | `apply_macro_to_entry_advice()` ужесточает `entry_advice` |
| Карточки / API | `get_5m_card_payload()` | Поля `macro_*`, `entry_advice_reason_local` |
| Премаркет Telegram | `scripts/premarket_cron.py` | Алерт risk-off / favorable (флаг `PREMARKET_STRESS_USE_MACRO_RISK`) |
| Исполнение входа 5m | `scripts/send_sndk_signal_cron.py` | Опц. блок первого BUY при `entry_advice=AVOID` (`GAME_5M_MACRO_BLOCK_NEW_BUY_ON_AVOID`) |
| Телеметрия сделок | `services/deal_params_5m.py` | `macro_*` в `context_json` на BUY |
| Арбитр идей | `services/analyzer_product_ideas_arbiter.py` | Вердикт по закрытым сделкам в `/api/analyzer` |

**Важно:** по умолчанию `entry_advice` (ALLOW / CAUTION / AVOID) — **совет** на карточках и в Telegram. Крон **не** смотрит на макро, пока не включён **`GAME_5M_MACRO_BLOCK_NEW_BUY_ON_AVOID=true`** — тогда при **AVOID** и **отсутствии** открытой позиции новый long не открывается (докуп при позиции и выходы не затрагиваются).

Нефть **вниз** сама по себе **не** даёт risk-off (в отличие от legacy `PREMARKET_STRESS_GAP_PCT` по всем тикерам).

---

## Логика risk-off и favorable

### Risk-off (AVOID)

| Сигнал | Условие (дефолт) | Вес в `risk_score` |
|--------|------------------|---------------------|
| VIX | гэп ≥ +1.5% | +2 |
| Forex (GBP/EUR) | худший гэп ≤ −1.0% | +2 |
| Нефть CL=F | гэп ≥ +2.0% (вверх) | +1 |

`risk_score ≥ 2` → **`risk_level=AVOID`**, `equity_gap_bias=DOWN`, `close_game_alert=true` (для премаркет-крона).

`risk_score == 1` → **CAUTION**, bias DOWN (алерт закрытия — по `PREMARKET_STRESS_ALERT_ON_CAUTION`).

### Favorable (ожидание гэпа вверх по риск-активам)

| Сигнал | Условие |
|--------|---------|
| VIX | гэп ≤ −1.0% |
| Forex | лучший гэп ≥ +0.5% при отсутствии слабого forex |
| Нефть | гэп ≤ −1.5% и VIX < +1.0% |

`favorable_score ≥ 2` → CAUTION + bias **UP**; при score ≥ 1 — bias **UP** (подсказка в `entry_advice`, без AVOID).

### Источник гэпа

`get_indicator_gap_detail(ticker)`: премаркет (`PRE_MARKET`) → `premarket` + `premarket_last` / `prev_close`; иначе два последних close в `quotes` → `quotes_2d`.

В Telegram (макро favorable / risk-off) при `GAME_5M_MACRO_INCLUDE_GAME_5M_GAPS_IN_TELEGRAM=true` добавляется блок **GAME 5m (премаркет)** по всем тикерам `get_tickers_game_5m()`.

### Точность OLS-прогноза гэпа сектора (`macro_predicted_sector_gap_pct`)

Офлайн: `scripts/analyze_macro_gap_indicators.py` (≈400 торговых дней, SMH и др.):

| Что предсказываем | Типичный R² (макро-гэпы → equity) | Вывод |
|-------------------|-----------------------------------|--------|
| **Гэп на открытие RTH** (`Open/PrevClose`) | **~0.4–0.5** | Умеренная связь; порядок величины полезен как **ориентир**, не как точный % |
| Просадка от open до low дня | ~0.01 | Макро-гэпы **не** предсказывают внутридневной drawdown |
| Первые 30 мин RTH | ~0.01 | То же для раннего выхода |

Коэффициенты в `GAME_5M_MACRO_PREDICT_*` — из OLS **SMH**, n≈326 совпадающих дней (см. комментарий в коде). Это **не** walk-forward и **не** калибровка по вашим сделкам GAME_5m — вердикт по сделкам: `product_ideas_arbiter` → `macro_predicted_sector_gap`.

**Ограничения live:**

- В проде на вход OLS идут **премаркет-гэпы** VIX/Forex/нефти; в обучении — **дневной** гэп на open (близко, но не идентично).
- Прогноз относится к **прокси** (`GAME_5M_MACRO_SECTOR_PROXY`, по умолчанию SMH), не к каждому тикеру игры; гэпы **ваших** тикеров — отдельные строки в алерте.
- При R²≈0.45 типичная ошибка по модулю — **порядка 1–2 п.п.** и выше; знак совпадает чаще, чем случайно, но не всегда.

Переоценивать точность не стоит: использовать как **вес к макро-bias** и сравнение с фактическими гэпами GAME_5m в том же сообщении.

### Калибровка по факту open (пайплайн как multiday gates)

| Шаг | Действие |
|-----|----------|
| 1 | `GAME_5M_GAP_FORECAST_LOG_ENABLED=true`, DDL: `python scripts/ingest_game5m_gap_forecast.py --ensure-table` |
| 2 | PRE_MARKET: `premarket_cron` → `record_premarket_gap_snapshots()` (pred + гэп тикеров в `game5m_gap_forecast_daily`) |
| 3 | После 9:30 ET: `ingest_game5m_gap_forecast.py --phase open` или лениво из `get_decision_5m` |
| 4 | Анализатор: `game5m_gap_forecast_arbiter` — MAE, знак, `insufficient_data` / `ready_for_coef_update` |
| 5 | Офлайн refit: `analyze_game5m_gap_forecast.py --days 120 --suggest-coefs` → обновить `GAME_5M_MACRO_PREDICT_*` вручную |

Вердикты арбитра: `insufficient_data` → `accumulating` → `caution` → **`ready_for_coef_update`** (не меняет config автоматически).

---

## Поля на карточке и в API

| Поле | Описание |
|------|----------|
| `entry_advice` | Итог: ALLOW / CAUTION / AVOID (новости, вола 5m, премаркет, **макро**) |
| `entry_advice_reason` | Полная причина (для Telegram / совместимости) |
| `entry_advice_reason_local` | Только **локальные** причины (новости, вола, премаркет) **до** макро |
| `macro_risk_level` | NEUTRAL / CAUTION / AVOID |
| `macro_equity_gap_bias` | NEUTRAL / DOWN / UP |
| `macro_risk_reasons` | Список строк (VIX, Forex, нефть — с порогами) |
| `macro_indicators` | `{ticker: gap_pct}` |
| `macro_predicted_sector_gap_pct` | Опционально, если включён OLS-прогноз |

**UI 5m · карточки:** строка **«Вход»** — совет + `entry_advice_reason_local`; строка **«Макро»** — уровень, bias и все `macro_risk_reasons` (без дубля в «Вход»).

---

## Конфиг (`config.env`)

См. `config.env.example`:

| Ключ | Дефолт | Смысл |
|------|--------|--------|
| `GAME_5M_MACRO_RISK_ENABLED` | `true` | Включить расчёт |
| `GAME_5M_MACRO_BLOCK_NEW_BUY_ON_AVOID` | `false` | При `true`: крон не открывает **первый** long, если `entry_advice=AVOID` (макро/новости/вола); докуп не блокируется |
| `GAME_5M_MACRO_FOREX_TICKERS` | `GBPUSD=X,EURUSD=X` | Forex для макро |
| `GAME_5M_MACRO_VIX_TICKER` | `^VIX` | VIX |
| `GAME_5M_MACRO_OIL_TICKER` | `CL=F` | Нефть |
| `GAME_5M_MACRO_*_GAP_*` | см. example | Пороги avoid/favorable |
| `PREMARKET_STRESS_USE_MACRO_RISK` | `true` | Новые премаркет-алерты |
| `PREMARKET_FAVORABLE_ALERT_TELEGRAM` | `false` | Алерт «гэп вверх» |
| `GAME_5M_MACRO_INCLUDE_GAME_5M_GAPS_IN_TELEGRAM` | `true` | Блок гэпа/цены по тикерам GAME_5m в макро-алерте |
| `GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED` | `true` | OLS-прогноз **величины** гэпа SMH (VIX/Forex/нефть → %); в Telegram и карточках |

Legacy: `PREMARKET_STRESS_USE_MACRO_RISK=false` — старый алерт «любой гэп ≤ −1.5%» по `PREMARKET_STRESS_TICKERS`.

**Данные:** в `TICKERS_LONG` должны быть `^VIX`, `GBPUSD=X`, `EURUSD=X`, `CL=F` (обновление: `update_prices_cron`).

---

## Порядок внедрения

### Фаза 0 — исследование (готово)

1. Запустить `scripts/analyze_macro_gap_indicators.py` на VM (400d, SMH/QQQ/SNDK).
2. Зафиксировать: VIX/Forex объясняют **гэп на open** (R² ~0.4–0.5); **просадка дня / первые 30m** — почти не предсказуемы макро-гэпами (R² ~0.01).
3. Вывод: макро — **фильтр входа / алерт**, не замена exit-логики.

### Фаза 1 — наблюдение (текущий прод)

1. `GAME_5M_MACRO_RISK_ENABLED=true` (дефолт в коде).
2. Карточки 5m и Telegram показывают AVOID/CAUTION и блок «Макро».
3. При желании согласовать карточку с ботом: `GAME_5M_MACRO_BLOCK_NEW_BUY_ON_AVOID=true` в `config.env` (только первый вход; выходы без изменений).
4. Премаркет: `PREMARKET_STRESS_USE_MACRO_RISK=true` — risk-off в Telegram утром.

**Критерий перехода:** ≥2 недели, в BUY `context_json` есть `macro_risk_level` на большинстве новых сделок.

### Фаза 2 — арбитр анализатора

1. Раз в 3–7 дней: `/api/analyzer?strategy=GAME_5M&days=7…14`.
2. Смотреть JSON `product_ideas_arbiter` → `macro_vix_forex_risk`:
   - `insufficient_data` — копить;
   - `caution` — пилот / калибровка порогов;
   - `keep` — оставить как ориентир;
   - `remove` — отключить `GAME_5M_MACRO_RISK_ENABLED` или ослабить пороги.
3. Не путать с `ml_production_arbiter` (CatBoost / ridge).

### Фаза 3 — калибровка порогов (по данным)

1. При устойчивом `remove` или ложных AVOID — подстроить `GAME_5M_MACRO_VIX_GAP_AVOID_PCT` и др. в `config.env` на VM.
2. Повторить офлайн `analyze_macro_gap_indicators.py` после смены окна данных.

### Фаза 4 — песочница прогноза гэпа (опционально)

1. Включить только после фазы 2: `GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED=true`.
2. Коэффициенты OLS — из отчёта SMH или переопределение в config.
3. Арбитр: `macro_predicted_sector_gap` в `product_ideas_registry`.

### Фаза 5 — осторожно

- **Блок первого BUY при AVOID** — опционально через `GAME_5M_MACRO_BLOCK_NEW_BUY_ON_AVOID` (см. выше); не смешивать с выходами и recovery без арбитра.
- Defer `TIME_EXIT_EARLY` по макро UP (идея `macro_defer_time_exit_early` — только sandbox + арбитр; recovery D4a на текущих данных defer **не** поддерживает).

---

## Деплой и проверка

```bash
# Локально
git push origin main

# VM
ssh ai8049520@104.154.205.58 "cd /home/ai8049520/lse && ./scripts/deploy_from_github.sh"
```

**Smoke после деплоя:**

```bash
ssh ai8049520@104.154.205.58 "docker exec lse-bot bash -lc 'cd /app && python3 -c \"
from services.macro_premarket_risk import evaluate_macro_premarket_risk
print(evaluate_macro_premarket_risk())
\"'"
```

**Карточки:** `/game5m/cards` — при risk-off: «Вход» = AVOID без повтора VIX в подстроке; «Макро» = полный список причин включая нефть.

**Анализатор:**

```bash
curl -sS "http://104.154.205.58:8080/api/analyzer?days=14&strategy=GAME_5M" | jq '.product_ideas_arbiter'
```

---

## Офлайн-анализ (гэп, просадка, регрессия)

Скрипт `scripts/analyze_macro_gap_indicators.py`:

- **gap_open** — гэп на открытие RTH;
- **drop_day_low**, **drop_first30m** — просадки;
- корреляции с гэпами VIX, GBP, EUR, CL;
- OLS: `equity_gap ~ VIX + Forex + oil`.

```bash
docker exec lse-bot bash -lc 'cd /app && python scripts/analyze_macro_gap_indicators.py --days 400 --equities SMH,QQQ,SNDK'
```

Артефакты: `docs/reports/macro_gap_panel_*.csv`, `macro_gap_regression_summary.json`.

---

## Команды Telegram

- `/corr5m` — тикеры игры; для пары с макро: `/corr5m SNDK ^VIX`.
- Премаркет-алерт при `close_game_alert` — текст из `format_macro_telegram_lines()`.

---

## См. также

- [GAME_5M_PRODUCT_IDEAS_ARBITER.md](GAME_5M_PRODUCT_IDEAS_ARBITER.md) — реестр идей и вердикты
- [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md) — полный отчёт `/analyzer`
- [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md) — статус направления в roadmap

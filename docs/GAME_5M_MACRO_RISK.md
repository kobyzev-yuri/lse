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
| Телеметрия сделок | `services/deal_params_5m.py` | `macro_*` в `context_json` на BUY |
| Арбитр идей | `services/analyzer_product_ideas_arbiter.py` | Вердикт по закрытым сделкам в `/api/analyzer` |

**Важно:** `entry_advice` (ALLOW / CAUTION / AVOID) — **совет оператору** на карточках и в Telegram. Крон `game_5m.py` **не блокирует** BUY только из-за `entry_advice` или макро.

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

`get_indicator_gap_pct(ticker)`: премаркет (`PRE_MARKET`) → `premarket`; иначе два последних close в `quotes` → `quotes_2d`.

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
| `GAME_5M_MACRO_FOREX_TICKERS` | `GBPUSD=X,EURUSD=X` | Forex для макро |
| `GAME_5M_MACRO_VIX_TICKER` | `^VIX` | VIX |
| `GAME_5M_MACRO_OIL_TICKER` | `CL=F` | Нефть |
| `GAME_5M_MACRO_*_GAP_*` | см. example | Пороги avoid/favorable |
| `PREMARKET_STRESS_USE_MACRO_RISK` | `true` | Новые премаркет-алерты |
| `PREMARKET_FAVORABLE_ALERT_TELEGRAM` | `false` | Алерт «гэп вверх» |
| `GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED` | `false` | OLS-прогноз гэпа SMH (песочница) |

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
3. **Не** менять `game_5m.py` для блокировки BUY по макро.
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

### Фаза 5 — НЕ делать без отдельного эксперимента

- Блокировать BUY в кроне по `entry_advice=AVOID`.
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

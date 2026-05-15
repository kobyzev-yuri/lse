# GAME_5m: макро VIX / Forex / нефть

## Назначение

`services/macro_premarket_risk.py` — единые правила для:

- **entry_advice** на карточках 5m (`AVOID` / `CAUTION` / подсказка «гэп вверх»);
- **премаркет-алертов** Telegram (`premarket_cron.py`).

Нефть **вниз** сама по себе **не** даёт risk-off (в отличие от legacy `PREMARKET_STRESS_GAP_PCT` по всем тикерам).

## Risk-off (AVOID → алерт «закрыть GAME_5m»)

| Сигнал | Условие (дефолт) | Вес |
|--------|------------------|-----|
| VIX | гэп ≥ +1.5% | +2 |
| Forex (GBP/EUR) | худший гэп ≤ −1.0% | +2 |
| Нефть CL=F | гэп ≥ +2.0% (вверх) | +1 |

`risk_score ≥ 2` → **AVOID**, `close_game_alert=true`.

## Favorable (ожидание гэпа вверх)

| Сигнал | Условие |
|--------|---------|
| VIX | гэп ≤ −1.0% |
| Forex | лучший гэп ≥ +0.5% при отсутствии слабого forex |
| Нефть | гэп ≤ −1.5% и VIX < +1.0% |

`favorable_score ≥ 2` → **CAUTION** + bias **UP**; при score ≥ 1 — bias **UP** (подсказка в entry_advice).

## Конфиг

См. `config.env.example`: `GAME_5M_MACRO_*`, `PREMARKET_STRESS_USE_MACRO_RISK`, `PREMARKET_FAVORABLE_ALERT_TELEGRAM`.

Legacy: `PREMARKET_STRESS_USE_MACRO_RISK=false` — старый алерт «любой гэп ≤ −1.5%» по `PREMARKET_STRESS_TICKERS`.

## Анализ истории (гэп, просадка, регрессия)

Скрипт `scripts/analyze_macro_gap_indicators.py`:

- **gap_open** — гэп на открытие RTH: `Open/PrevClose - 1` (%);
- **drop_day_low** — просадка от open до дневного Low (daily-прокси «плохого дня»);
- **drop_first30m** — min Low в 9:30–10:00 ET по 5m (`--intraday-days 60`);
- корреляции с гэпами **VIX, GBPUSD, EURUSD, CL=F**;
- **OLS**: `equity_gap ~ const + VIX_gap + Forex_gaps + oil_gap` (R², коэффициенты, p≈).

```bash
# на VM в контейнере
docker exec lse-bot bash -lc 'cd /app && python scripts/analyze_macro_gap_indicators.py --days 400 --equities SMH,QQQ,SNDK'
docker exec lse-bot bash -lc 'cd /app && python scripts/analyze_macro_gap_indicators.py --days 400 --equity SMH --intraday-days 60'
```

Артефакты: `docs/reports/macro_gap_panel_<ticker>_<n>d.csv`, `macro_gap_regression_summary.json`.

Интерпретация: при **R² &lt; 0.1** макро-гэпы слабо предсказывают equity gap — правила AVOID лучше держать консервативными; при устойчивых коэффициентах VIX/Forex можно калибровать пороги `GAME_5M_MACRO_*`.

## Команды Telegram

`/corr5m` — только тикеры игры. Для пары с макро: `/corr5m SNDK ^VIX` или полный LLM-универс в `recommend5m` GAME5M.

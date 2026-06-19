# GAME_5M — лог настроек агента (Cursor)

Журнал **предложений** и **фактически применённых** изменений `config.env` на проде. Цель — согласовать ручную настройку с анализатором (`/api/analyzer`) и не терять контекст «почему так».

**Иерархия источников (как договорились):**
1. **Анализатор** — центральная система метрик и `auto_config_override` (см. [GAME_5M_TUNING_REGLEMENT.md](GAME_5M_TUNING_REGLEMENT.md)).
2. **Этот лог** — оперативные решения агента до/между прогонами анализатора; при расхождении приоритет у проверенных live-фактов + WF/OOS.
3. **Один параметр за эксперимент** — как в регламенте; observe 1–3 сессии перед следующим шагом.

**Связанные артефакты:** `local/game5m_tuning_ledger.json` (controller/replay), `last_multiday_wf_game5m.json` (WF v3nm).

**Ежедневный отчёт:**
- Скрипт: `scripts/game5m_daily_session_review.py` → `last_game5m_daily_session_review.json`
- Cron на VM: **включён** `35 23 * * 1-5` → `scripts/cron_game5m_daily_session_review.sh` (с 12.06.2026)
- В отчёте: late-buy cutoff из config (90 мин → MSK), EOD-flat `distance_to_take`, rolling gap PM vs ML 14/30d

---

## Сессия 2026-06-19 — entry bar v2 (shadow, без prod apply)

**ML roadmap:** [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md) §13–14.

**Факт на prod после deploy (`1922890`, `2a38338`):**
- Bar dataset 9625 rows, CatBoost v2 AUC(valid)=**0.5495** (порог promotion 0.55 — **не пройден**).
- `catboost_entry_proba_good_v2` — **log_only**; v1 trade CatBoost и fusion **без изменений**.

**На завтра (P0):** 1.7 trust arbiter digest; weekly retrain hook (фаза 5). **P1:** continuation ML 2.1–2.3. **Не делать:** 1.8 apply до AUC≥0.55 + ~2 нед telemetry.

---

## Сессия 2026-06-10 (US RTH, ещё не открылась)

**Состояние на утро MSK (pre-market probe ~10:45 UTC):**

| Параметр | Значение |
|----------|----------|
| `GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE` | `apply` |
| `GAME_5M_MULTIDAY_ENTRY_GATE_MODE` | `apply` |
| `GAME_5M_MULTIDAY_HOLD_GATE_MODE` | `log_only` |
| `GAME_5M_EOD_FLATTEN_ALWAYS` | `true` |
| `GAME_5M_BLOCK_NEW_BUY_NEAR_CLOSE_ENABLED` | `true` |
| `GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE` | **90** ← применено сегодня (было 60) |
| WF multiday v3nm | `ready` (2026-06-09) |

### Проверка `MULTIDAY_OVERNIGHT_GATE_MODE=apply`

Live `get_decision_5m` + `evaluate_multiday_overnight_gate` (pre-market 10 июня):

| Тикер | 1d / 2d / 3d % vs spot | `would_avoid_overnight` | Примечание |
|-------|------------------------|-------------------------|------------|
| **SNDK** | −0.36 / −1.10 / −1.99 | **true** | 1d < −0.25%; 3 негативных горизонта |
| NBIS | +0.49 / +0.67 / +1.14 | false | бычий multiday |
| ASML | −0.05 / −0.01 / −0.02 | false | слабо нейтральный |
| MU | +0.21 / +0.58 / +0.82 | false | |
| LITE | +0.92 / +0.94 / +1.35 | false | |
| CIEN | +0.50 / +0.84 / +1.14 | false | |
| TER | +0.02 / +0.23 / +0.34 | false | |
| AMD | +0.20 / +0.26 / +0.38 | false | |

Симуляция REGULAR (−50 / −30 мин до close): **SNDK** — `block_new_buy=true`, `overnight_avoid=true`, EOD-flat при ≤20 мин — **цепочка согласована**.

**Вывод:** overnight gate в режиме `apply` **работает** на уровне оценки; влияние на сделки:
- **EOD:** `evaluate_multiday_overnight_risk` → `should_eod_flatten` (при `EOD_FLATTEN_ALWAYS=true` flat всех за 20 мин независимо от multiday).
- **Premarket:** `should_premarket_auto_flat` при гэпе ≤ −2% и/или медвежьем multiday.
- **Новый вход:** `should_block_new_buy_for_overnight` — near-close + bearish multiday.

**Инцидент 09.06:** поздние BUY 21:05–21:55 MSK при окне 60 мин — вероятно `minutes_until_close` не попал в `market_session` в момент крона (в BUY `context_json` поле `null`). Мониторим логи `пропуск нового входа — overnight policy (near_close_...)`.

### Предложения (очередь)

| ID | Параметр | Предложение | Статус | Обоснование | Согласование с анализатором |
|----|----------|-------------|--------|-------------|----------------------------|
| P-2026-06-10-01 | `GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE` | 60 → **90** | **applied** | Поздние входы 09.06 → EOD-flat без времени на тейк | Пока нет replay; согласуется с разбором TIME_EXIT |
| P-2026-06-10-02 | `GAME_5M_EOD_FLATTEN_ALWAYS` | `true` → `false` + bullish hold exception | **deferred** | LITE 09.06: бычий multiday, flat −1.15% | Ждём 2–3 сессии после P-01 |
| P-2026-06-10-03 | `GAME_5M_MULTIDAY_HOLD_GATE_MODE` | `log_only` → `apply` | **rejected** | WF ready, но <5 would_defer в арбитре | [GAME_5M_MULTIDAY_LR_GATES_ROLLOUT_PLAN.md](GAME_5M_MULTIDAY_LR_GATES_ROLLOUT_PLAN.md) |
| P-2026-06-10-04 | per-ticker entry τ для AMD | ужесточить 1d τ | **deferred** | WF h1 sign 51% | После накопления BUY с gate telemetry |

### Применено на прод (2026-06-10, pre-RTH)

```env
GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE=90
```

Остальное без изменений.

### Чеклист наблюдения за сессией 10.06

После закрытия RTH проверить:

- [ ] Нет новых BUY после ~14:30 ET (≈21:30 MSK) — блок 90 мин.
- [ ] В логах крона есть `overnight policy (near_close_...)` при попытках позднего входа.
- [ ] TIME_EXIT: доля `overnight_eod_flat` vs 09.06 (ожидаем **меньше** поздних входов → меньше мелких EOD-flat).
- [ ] SNDK: при медвежьем multiday — нет нового long; premarket flat если гэп/ multiday.
- [ ] Прогон `/api/analyzer?strategy=GAME_5M&days=3` — сравнить с этим логом.

### Post-session review (10.06)

| Метрика | Baseline (08–09.06) | 10.06 | Вывод |
|---------|---------------------|-------|-------|
| TIME_EXIT n / avg PnL | 4 / ~0% | 3 / **−0.54%** | EOD-flat всё ещё слабый |
| Поздние BUY (≥21:30 MSK) | 4 (09.06) | **1** | P-01 начал работать |
| TIME_EXIT_EARLY avg PnL | −8% (09.06) | **−2.88%** | улучшение |

**Коррекция гипотез:** P-01 держать; P-02 отложено.

### Post-session review (11.06)

| Метрика | 10.06 | 11.06 | Вывод |
|---------|-------|-------|-------|
| closes | 4 | **7** | |
| TIME_EXIT n / avg PnL | 3 / −0.54% | 6 / **+2.46%** | сильный бычий день |
| TIME_EXIT_EARLY | 1 | **0** | ✓ |
| Late BUY (cutoff 21:30) | 1 | **0** | P-01 подтверждён |
| EOD near take (<1%) | — | **ASML** | наблюдать P-02 |

---

## Ops 2026-06-12 — ML + session review (деploy)

### Применено на прод (config.env + cron + код)

```env
GAME_5M_TICKER_OPEN_GAP_PREMARKET_BLEND_WEIGHT=0.60
GAME_5M_OPEN_GAP_FORECAST_BLEND_ML_WEIGHT=0.20
ML_READINESS_GAME5M_MIN_VALID=80
# GAME_5M_CATBOOST_ENABLED=false  — не менять (n_valid=49)
```

**Код:** rolling gap MAE 14/30/90d; policy `auto` → PM baseline пока ML не beat PM на 14d+30d; daily review + gap metrics.

**Cron (`crontab/lse-docker.crontab`):**
- `35 23 * * 1-5` — `cron_game5m_daily_session_review.sh`
- `50 16 * * 1-5` — `analyze_game5m_gap_forecast.py --days 90`
- `25 6 * * 0` — `run_multiday_wf_game5m.py`

### Proposals (ML, 12.06)

| ID | Изменение | Статус |
|----|-----------|--------|
| ML-12-01 | gap blend 0.45→0.60, effective=policy auto | **applied** |
| ML-12-02 | entry CatBoost L3 | **deferred** (AUC 0.58, n_valid 49<80) |
| ML-12-03 | multiday hold apply | **rejected** (would_defer<5) |
| ML-12-04 | P-02 EOD_FLATTEN_ALWAYS=false | **deferred** |

---

## Шаблон следующей записи

```markdown
## Сессия YYYY-MM-DD

### Pre-session
- config snapshot
- overnight gate probe (optional: docker exec …)

### Proposals
| ID | key | change | status | rationale |

### Applied
- ...

### Post-session review
- analyzer days=N
- keep / rollback / next proposal
```

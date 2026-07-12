# GAME_5M — лог настроек агента (Cursor)

Журнал **предложений** и **фактически применённых** изменений `config.env` на проде. Цель — согласовать ручную настройку с анализатором (`/api/analyzer`) и не терять контекст «почему так».

**Иерархия источников (как договорились):**
1. **Анализатор** — центральная система метрик и `auto_config_override` (см. [GAME_5M_TUNING_REGLEMENT.md](GAME_5M_TUNING_REGLEMENT.md)).
2. **Этот лог** — оперативные решения агента до/между прогонами анализатора; при расхождении приоритет у проверенных live-фактов + WF/OOS.
3. **Песочница (prod VM):** эксперименты **apply-first** — без лишних фаз `log_only`/observe, если гипотеза ясна и откат тривиален. `log_only` только для чистого ML-shadow без влияния на PnL. Один **смысловой** пакет за раз (bundle), не один ключ в вакууме.

**Связанные артефакты:** `local/game5m_tuning_ledger.json` (controller/replay), `last_multiday_wf_game5m.json` (WF v3nm).

**Ежедневный отчёт:**
- Скрипт: `scripts/game5m_daily_session_review.py` → `last_game5m_daily_session_review.json`
- Post-mortem: `scripts/game5m_trade_postmortem.py` → JSONL + `last_game5m_postmortem_tactics.json` (cron вместе с review)
- **Чеклист оператора:** [GAME_5M_SESSION_ANALYSIS_PLAYBOOK.md](GAME_5M_SESSION_ANALYSIS_PLAYBOOK.md)
- Cron на VM: **включён** `35 23 * * 1-5` → `scripts/cron_game5m_daily_session_review.sh` (с 12.06.2026)
- В отчёте: late-buy cutoff из config (90 мин → MSK), EOD-flat `distance_to_take`, rolling gap PM vs ML 14/30d

---

## Сессия 2026-07-12 — freeze B1–B6 ML (no-go)

**Решение:** bar v2 fusion sweep no-go; B1–B6 без PnL-edge — закрыты.

**Bundle `ml_freeze_b_contours_v1` на prod:**
- `GAME_5M_CATBOOST_FUSION=none`
- `GAME_5M_HOLD_QUALITY_LOG_ENABLED=false` (B1)
- `GAME_5M_CONTINUATION_ML_ENABLED=false`, `GATE_MODE=none` (B2)
- `GAME_5M_MULTIDAY_HOLD_GATE_MODE=none` (B3)
- `DAILY_ML_RUN_ENTRY_BAR_V2_APPLY=0`, `DAILY_ML_RUN_CONTINUATION_DATASET=0`
- `ML_READINESS_SKIP_GAME5M=1`, `ML_READINESS_SKIP_EARNINGS_INTELLIGENCE=1`

**Cron отключён:** light analyzer (B6), earnings_prod_eval (B5), dispatcher frozen contours (entry/bar_v2/continuation/earnings_grid).

**Остаётся в prod:** `market_adapt_v1` guards, multiday **entry** apply, portfolio CatBoost.

---

## Сессия 2026-06-20 — continuation ML telemetry ON (prod)

**Включено на VM (`config.env`):**
- `GAME_5M_CONTINUATION_ML_ENABLED=true`
- `GAME_5M_CONTINUATION_ML_LOG_ONLY=true`
- `GAME_5M_CONTINUATION_ML_GATE_MODE=log_only`

**Fix после первого probe:** CatBoost predict через `Pool` + cat_features (был `invalid index to scalar variable`).

**Ожидание:** в SELL `TAKE_PROFIT*` → `exit_context_json.continuation_ml` с `continuation_proba`, `would_defer_take`, `log_only=true`. Apply **не** включаем.

---

## Сессия 2026-06-19 — entry bar v2 (shadow, без prod apply)

**ML roadmap:** [GAME_5M_PREDICTOR_DATASET_PLAN.md](GAME_5M_PREDICTOR_DATASET_PLAN.md) §13–14.

**Факт на prod после deploy (`1922890`, `2a38338`):**
- Bar dataset 9625 rows, CatBoost v2 AUC(valid)=**0.5495** — promotion gate **0.545 пройден** (ослаблен с 0.55).
- `catboost_entry_proba_good_v2` — **log_only**; v1 trade CatBoost и fusion **без изменений**.

**На завтра (P0):** 1.7 trust arbiter digest; weekly retrain hook (фаза 5). **P1:** continuation ML 2.1–2.3. **Не делать:** 1.8 apply до ~2 нед telemetry + ops sign-off (AUC gate OK, fusion всё ещё shadow).

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
| P-2026-06-10-01 | `GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE` | 60 → **90** | **superseded** | Поздние входы 09.06 → EOD-flat без времени на тейк | Пока нет replay; согласуется с разбором TIME_EXIT |
| P-2026-06-22-01 | `GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE` | 90 → **30** | **applied** | 90 мин блокировал BUY/STRONG_BUY (LITE/CIEN) при ~58 мин до close; EOD-flat остаётся за 20 мин | Ослабление near-close; bearish multiday block без изменений |
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

## Сессия 2026-06-25 — intraday regime router

**Статус:** **applied** на prod (deploy `c994205`, config через `update_config_key`, 2026-06-25 ~14:15 MSK).

**Код:** `services/game5m_intraday_regime.py` — классификатор `chop` / `impulse_up` / `fade_extended` / `neutral`.

| Режим | Вход | Выход |
|-------|------|-------|
| `chop` | блок `buy_rth_momentum` при RTH < 1.5% | take cap ×0.85, factor ×0.9, soft-take 2% в REGULAR, EOD −0.35% |
| `impulse_up` | без блока | momentum factor ×1.15 |
| `fade_extended` | BUY/STRONG_BUY → HOLD | — |

**Config на проде:**
```env
GAME_5M_INTRADAY_REGIME_ENABLED=true
GAME_5M_INTRADAY_REGIME_GATE_MODE=apply
# + chop/impulse multipliers (см. config.env.example)
```

**Post-session checklist (1–3 RTH после deploy):**
- [ ] В BUY `context_json`: `intraday_regime.regime`, при chop — `intraday_regime_entry_guard_triggered=true` на слабом momentum
- [ ] Меньше входов `buy_rth_momentum` с RTH 1.2–1.5% в chop-дни vs 24.06
- [ ] В chop: soft-take / EOD-flat `overnight_eod_flat_loss` при −0.35% (не −0.5%)
- [ ] В impulse_up: доля TAKE_PROFIT / выше avg realized на тейках
- [ ] `game5m_daily_session_review.py` + `/api/analyzer?days=3` — сравнить с 16–24.06

**Не трогать параллельно:** пороги `RTH_MOMENTUM_BUY_MIN`, EOD multiday, block near-close — пока не оценён этот пакет.

**Техдолг:** `apply-bundle intraday_regime_v1` падает на `negative_value_not_allowed` для `CHOP_EOD_MAX_LOSS`; ledger всё ещё `overnight_multiday_v1` pending — не блокирует работу режима.

---

## Сессия 2026-07-06 — pre-session: rollback overnight + entry fusion tighten

**Контекст:** post-mortem 3 сессий (01–03.07), rolling A=5 fusion_fp=3; MU/TER `buy_premarket_momentum` P≈0.515.

### Pre-session (до RTH 06.07)

| Действие | Результат |
|----------|-----------|
| **Rollback** `overnight_multiday_v1` (pending 19d, observe 5d log-ret −0.32) | `rolled_back` |
| **Apply** `entry_fusion_tighten_v1` | `pending_effect` |

**Изменения config.env (prod):**

| Ключ | Было | Стало |
|------|------|-------|
| `GAME_5M_CATBOOST_HOLD_BELOW_P` | 0.45 | **0.50** |
| `GAME_5M_PREMARKET_MOMENTUM_BUY_MIN` | (default 0.5) | **1.0** |

**Откат overnight bundle восстановил:**

| Ключ | После rollback |
|------|----------------|
| `GAME_5M_EOD_FLATTEN_ALWAYS` | **true** |
| `GAME_5M_MULTIDAY_HOLD_GATE_MODE` | **log_only** |

**Не меняли (следующий цикл):** exit bundle / `TAKE_PROFIT_MIN_PCT` replay propose.

**Открытые позиции на утро:** LITE, TER (BUY 02.07 22:55).

**Следующая проверка:** observe после ≥8 закрытий; вечерний post-mortem cron 23:35 MSK.

**Experiment id:** `bundle:entry_fusion_tighten_v1@2026-07-06T12:40:11Z`

---

## Сессия 2026-07-11 — market_adapt_v1: legacy guards + bar v2 learning

**Контекст:** analyzer 7d — 8/11 loss days с gap ≤ −2%; stack `premarket_gap_baseline` HOLD, legacy BUY (RESOLVE=false). Bar v2 на fusion (P≈0.51) — fusion ineffective.

### Изменения кода

| Компонент | Что |
|-----------|-----|
| `services/game5m_entry_guards.py` | Legacy apply: `GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE`, `GAME_5M_ENTRY_ADVICE_GATE_MODE` |
| `recommend_5m.py` / `game5m_policy.py` | Вызов после multiday |
| Bundle `market_adapt_v1` | gap/advice apply + intraday_regime + `TAKE_PROFIT_MIN_PCT=1.5` |
| Daily ML pipeline | `run_game5m_entry_bar_v2_ml_refresh.py --apply-data` (train — weekly dispatcher) |

**Apply на prod:** `game5m_tuning_controller.py apply --bundle-id market_adapt_v1`

**Observe:** ≥5 сессий; counterfactual 07.07 (CIEN/AMD/MU/TER/LITE при gap ≤ −2% → HOLD).

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

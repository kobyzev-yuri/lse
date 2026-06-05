# План консолидации ML-архитектуры

**Статус:** утверждённый план внедрения (2026-06-05).  
**Канон:** [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md).

## Выбранная стратегия

**«Single Canon + Tiered Deep-Dives + Archive Snapshots»**

Почему это наиболее эффективно:

| Альтернатива | Минус |
|--------------|-------|
| Переписать все 20+ md сразу | Долго, ломает ссылки, дрейф через неделю |
| Только код без доков | Ops продолжит читать устаревшие планы |
| Один mega-файл на 500 строк | Неудобно сопровождать, дубли с deep-dive |

**Решение:** один канон (~200 строк) + существующие deep-dive **без статуса «плана»** + dated snapshots в `docs/archive/` со stub-редиректами.

---

## Фазы внедрения

### Фаза 0 — Документация (1 PR, **сейчас**)

| # | Задача | DoD |
|---|--------|-----|
| 0.1 | Канон `ML_AND_DECISION_ARCHITECTURE.md` | Матрица 8 контуров, 3 слоя, 3 поверхности, cron, promotion |
| 0.2 | Этот rollout plan | Фазы 0–3 с DoD |
| 0.3 | Обновить `PROJECT_STATUS_AND_ROADMAP.md` | Дата 2026-06-05, unified framework, resolve=false |
| 0.4 | Обновить `docs/README.md`, `ARCHITECTURE.md` | Ссылка на канон вверху ML-секции |
| 0.5 | Stub / archive dated plans | WEEKLY_PLAN, EARNINGS_PLAN_2026-05-*, EARNINGS_INTELLIGENCE_PLAN |
| 0.6 | Баннер *Superseded for contour/cron matrix* | `DECISION_STACK_ROLLOUT`, `ML_UNIFIED`, `OPEN_PATH` — ссылка на канон |

**Не делать в фазе 0:** менять cron на prod, рефакторить train-скрипты.

---

### Фаза 1 — Устранение противоречий в L1 ✅ (2026-06-05)

| # | Задача | DoD |
|---|--------|-----|
| 1.1 | Watermark `game5m_entry` / `portfolio` = closed (SELL ts) | `count_strategy_closed_since`, `tests/test_ml_contour_deltas.py` |
| 1.2 | `run_daily_game5m_ml_pipeline.py` → datasets only | Train via `run_game5m_entry_ml_refresh`; `DAILY_ML_RUN_CATBOOST=1` legacy |
| 1.3 | Dispatcher `--slot nightly` | Cron `23:47`; заменяет 23:46/51/52 |
| 1.4 | Per-contour lock | `flock` в dispatcher, документировано в ML_UNIFIED |

**Критерий готовности:** один ночной путь train per contour; нет двойного full train game5m + readiness.

---

### Фаза 2 — Закрыть registry gaps ✅ (2026-06-05)

| # | Задача | DoD |
|---|--------|-----|
| 2.1 | `run_multiday_lr_ml_refresh.py` | ACTIVE + `last_multiday_lr_train_metrics.json` ✅ |
| 2.2 | `run_recovery_ml_refresh.py` | export + train + metrics ✅ |
| 2.3 | `run_gap_forecast_refresh.py` | analyze + `last_gap_forecast_metrics.json` ✅ |
| 2.4 | Multiday в `ml_train_readiness.jsonl` | advisory block `multiday_lr` ✅ |

**Критерий:** все 8 контуров в `ml_contours_status` с реальным `last_refresh`, не `registry-only`.

**Prod sign-off (2026-06-05):** `6607dad` → deploy → full train session; все 8 контуров `train_ran=True` в `ml_contours_status.json`. Дополнительно: `730db19` (archive legacy scripts), `8a03ea9` (recovery `--jsonl` / auto-pick). Сводка CatBoost/артефактов — см. [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md).

---

### Фаза 3 — L3 единый продукт (по контурам, не big-bang) — **в работе с 2026-06-05**

Порядок promotion (после L2 gate + фаза C):

| Очередь | Контур | Условие |
|---------|--------|---------|
| 1 | `catboost_entry_5m` | AUC + analyzer backtest; fusion уже осторожно |
| 2 | `multiday_lr` | walk-forward gates ([GAME_5M_MULTIDAY_LR_GATES_ROLLOUT_PLAN.md](GAME_5M_MULTIDAY_LR_GATES_ROLLOUT_PLAN.md)) |
| 3 | `premarket_gap_baseline` vs `gap_forecast` | ML только если beat baseline ([GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) §5) |
| 4 | `recovery_ml` | D4b go/no-go |
| 5 | `event_reaction` | advisory → optional veto после backtest |
| 6 | `DECISION_STACK_RESOLVE_ENABLED` | 3–7 дней mirror без divergence |

Earnings / open-path: **отдельная очередь** на shadow → product-tier ([EARNINGS_PRODUCT_ROADMAP.md](earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md)); не блокирует фазу 3 GAME_5M.

#### Спринт 3.0 — baseline и мониторинг (текущий)

| # | Задача | DoD |
|---|--------|-----|
| 3.0.1 | Baseline mirror: `decision_stack_shadow_diff` за 7 дн. | ✅ 32 snap / 4 div (12.5%), session veto |
| 3.0.2 | `scripts/report_decision_stack_mirror.py` | CLI: divergence rate, recent rows, exit 0/1 по порогу |
| 3.0.3 | Portfolio L3 | Уже `PORTFOLIO_CATBOOST_ENABLED=true` — формально **promoted** (L2✅ + L3✅) |
| 3.0.4 | Multiday entry gate | Prod сейчас `apply` — сверить с [GAME_5M_MULTIDAY_LR_GATES_ROLLOUT_PLAN.md](GAME_5M_MULTIDAY_LR_GATES_ROLLOUT_PLAN.md) §3.2 |
| 3.0.5 | `catboost_entry_5m` | AUC≈0.50 — **заблокирован** до фазы C (analyzer backtest) |

**Блокер RESOLVE:** `DECISION_STACK_RESOLVE_ENABLED=false` до 3–7 дней `resolve_divergence=0` (очередь 6).

---

### Фаза 4 — Ops и UI (backlog)

- `POST /api/ml/refresh?contour=`
- `continuous_learning` UI для `earnings_grid`
- `analyzer_contours` registry sync с `ML_CONTOUR_REGISTRY`
- Повторный prod config audit ([CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md](CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md))

---

## Что архивировать vs оставить

### → Archive (перенос + stub в старом пути)

| Файл | Причина |
|------|---------|
| `WEEKLY_PLAN_GAME5M_AND_ML_2026-05-06.md` | Снимок 2026-05-06 |
| `EARNINGS_PLAN_2026-05-29.md` … `2026-05-31.md` | Дневные снимки |
| `EARNINGS_INTELLIGENCE_PLAN.md` | Дубль roadmap + frontmatter todos устарели |

### Оставить (deep-dive, не план статуса)

- `ML_UNIFIED_RETRAIN_FRAMEWORK.md` — L1 контракт
- `ML_CALIBRATION_PHASES.md` — A–E
- `DECISION_STACK_ROLLOUT_PLAN.md` — имплементация L3
- `GAME_5M_*_PLAN.md` по recovery/hanger/multiday — **feature plans**, не матрица контуров
- `EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md` — рабочий чеклист earnings
- `OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md` — gates autoprep

### Живой статус (обновлять при каждом значимом деплое)

- `PROJECT_STATUS_AND_ROADMAP.md` — только таблицы «сделано / висит», без дублирования архитектуры

---

## Метрики успеха

| Метрика | Сейчас | Цель |
|---------|--------|------|
| Канонических entry-point для ML | 5+ | 1 (`ML_AND_DECISION_ARCHITECTURE`) |
| Контуров с auto L1 refresh | 8/8 ✅ | 8/8 |
| Nightly cron paths на contour | 2–3 дубля | 1 dispatcher slot |
| Dated «план» без stub в docs/ | 6+ | 0 |
| `DECISION_STACK_RESOLVE` | false | true после mirror sign-off |

---

## Порядок PR (рекомендуемый)

1. **PR-A (docs):** фаза 0 — канон, rollout, PROJECT_STATUS, README, archive stubs  
2. **PR-B (L1 fix):** фаза 1.1–1.2 — watermark + datasets-only legacy  
3. **PR-C (cron):** фаза 1.3 — nightly slot  
4. **PR-D (registry):** фаза 2 — multiday/recovery/gap refresh  
5. **PR-E+ (product):** фаза 3 — по одному контуру с sign-off

Не смешивать PR-A с prod cron changes — docs можно мержить сразу.

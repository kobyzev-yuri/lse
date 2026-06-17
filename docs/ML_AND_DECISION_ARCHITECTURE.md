# ML и торговые решения: каноническая архитектура

**Статус:** единая точка входа (обновлено 2026-06-07).  
**Аудитория:** разработка, ops, продукт.

**Словарь терминов:** английские идентификаторы и сокращения (L1/L2/L3, RESOLVE, shadow, BMO/AMH, spillover…) — [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md). В тексте ниже после первого упоминания часто даётся краткая расшифровка в скобках.

Этот документ **заменяет** разрозненные «планы недели» и дублирующие rollout-описания как **источник правды по контурам ML и их выходу в продукт**. Детали по сеткам, скриптам и UI остаются в профильных deep-dive (ссылки в §8).

**План устранения дублей и внедрения:** [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md).

---

## 1. Три слоя (без смешения)

Один контур ML проходит **три независимых слоя**. Cron и артефакты разные; путать слои — главный источник «противоречивой архитектуры».

| Слой | Вопрос | Инструменты | Артефакты |
|------|--------|-------------|-----------|
| **L1 — Retrain** (переобучение) | Пора ли пересобрать данные / `.cbm` (файл модели CatBoost)? | `run_ml_refresh_dispatcher.py` (единый планировщик), `run_*_ml_refresh.py` | `last_<contour>_ml_refresh.json`, `ml_contours_status.json` |
| **L2 — Quality gates** (пороги качества) | Достаточно ли данных и метрик (AUC, RMSE…)? | `run_ml_train_readiness_cron.py`, readiness writers | `ml_train_readiness.jsonl`, `last_*_readiness.json` |
| **L3 — Trading product** (влияние на сделку) | Влияет ли сигнал на сделку? | `decision_stack` (стек решений), `*_ENABLED`, `gate_mode` | `decision_snapshot` в `context_json`, `decision_effective` |

**Жёсткие правила:**

1. **L2 `ready=true` не включает L3** — не меняет `GAME_5M_CATBOOST_ENABLED`, `DECISION_STACK_RESOLVE_ENABLED` и т.п.
2. **L1 poll частый, train редкий** — `*/6` проверяет триггер; train только при Δ данных или `--full`.
3. **Promotion L2→L3** — отдельный чеклист (§6), не автоматика крона.

```text
  [факты: сделки, ERD, labels, quotes]
           │
           ▼
  L1 evaluate_retrain_trigger → apply-data / train / skip
           │
           ▼
  L2 readiness gates (JSONL + per-contour JSON)
           │
           ▼
  L3 decision_stack contributions → resolve → cron entry/hold/exit
```

Код L1: `services/ml_contour_refresh.py`, `services/ml_contour_deltas.py`, `services/ml_contour_runner.py`.  
Код L3: `services/decision_stack/`, `services/recommend_5m.py`.

### 1.1 Dual-track: legacy + decision_stack (параллельно)

**Legacy hot path** (текущий исполнитель: `technical_decision_effective`, portfolio guards) **исполняет сделки** и **уже включает** контуры с tier `promoted` / `legacy_apply` через свои флаги (`PORTFOLIO_CATBOOST_ENABLED`, `GAME_5M_MULTIDAY_ENTRY_GATE_MODE=apply`, …) — **без** `DECISION_STACK_RESOLVE_ENABLED=true`.

**Decision stack** (параллельный сбор вкладов) строит `decision_snapshot` параллельно. При `RESOLVE=false` (prod; stack не исполняет) — только **shadow** (лог без блока) и `projected_effective_if_resolve`; при `RESOLVE=true` — stack может стать единым исполнителем (session **veto** — запрет агрессивного входа — и др.).

`DECISION_STACK_OWN_FINALIZE=true` (default): CatBoost + multiday применяются в `apply_game5m_policy_gates()` **до** snapshot; результат попадает в legacy `technical_decision_effective`, который cron использует для входа.

**Gap open forecast:** `forecast_layer` / `forecast_open_gap_pct` = **Effective** (`pick_effective_open_gap_pct`, policy `auto`). Вход по гэпу — контур **`premarket_gap_baseline`** (PM gap + пороги), не поле Effective. Подробнее: [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) §5.

Реестр runtime: `services/ml_product_runtime.py`, CLI `scripts/print_ml_product_status.py`, отчёт [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md).

---

## 2. Три продуктовые поверхности

ML-контуры **не все** сходятся в один торговый hot path. Три поверхности:

| Поверхность | Игра / UI | Единая точка решения | Типичный ML |
|-------------|-----------|----------------------|-------------|
| **GAME_5M trading** | `GAME_5M`, карточки 5m | `decision_stack` → `decision_effective` | entry CatBoost, multiday ridge, gap, recovery, macro |
| **Portfolio trading** | `PORTFOLIO`, `/api/portfolio/cards` | `execution_agent` + карточки | portfolio CatBoost, multiday (planned) |
| **Earnings intelligence** | `/earnings`, Telegram brief | **Advisory** (подсказка) / **shadow** (только лог); **не** блокирует BUY по умолчанию | event regression (прогноз 5d), scenario classifier, **peer spillover** (реакция аналогов), **open-path** (первый час RTH) |

Open-path classifier — **MVP** (минимальный продукт) внутри earnings/open-play, **shadow** до закрытия product gates (`overall_open_path_classifier_ready`). Не путать с **event 5d** — см. [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) §5.

Канон GAME_5M: [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md).  
Earnings product: [earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md](earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md).

---

## 3. Реестр контуров (единая матрица)

Источник правды в коде: `ML_CONTOUR_REGISTRY` в `services/ml_contour_refresh.py`.

| `contour_id` | L1 refresh | L2 gates | L3 surface | `contour_id` в decision_stack | Runtime сейчас |
|--------------|------------|----------|------------|-------------------------------|----------------|
| `game5m_entry` | ✅ dispatcher | `ml_train_readiness.jsonl` → `game5m` | GAME_5M | `catboost_entry_5m` | fusion caution |
| `portfolio` | ✅ dispatcher | JSONL → `portfolio` | Portfolio | `portfolio_catboost` | карточки |
| `event_reaction_regression` | ✅ dispatcher | JSONL → `event_reaction` | Earnings advisory | `event_reaction` | off / advisory |
| `earnings_grid` | ✅ dispatcher | `last_earnings_intelligence_readiness.json` | Earnings UI | — | shadow |
| `open_path` | ✅ dispatcher | `last_open_path_readiness.json` | Open-path MVP | — | shadow |
| `multiday_lr` | ✅ dispatcher | arbiter + JSONL advisory | GAME_5M | `multiday_lr` | **legacy_apply** entry; hold log_only |
| `recovery` | ✅ dispatcher | D4a stats + metrics | GAME_5M exit | `recovery_ml` | log-only |
| `gap_forecast` | ✅ dispatcher | arbiter + metrics JSON | GAME_5M | `gap_forecast` | caution; baseline сильнее |

**Два слоя event ML (не смешивать):**

| Слой | Target | Features | Train | Роль |
|------|--------|----------|-------|------|
| Product regression | `forward_log_ret_5d` | `quotes_regime_v1` | `train_event_reaction_catboost.py` | Advisory 5d |
| Earnings grid | `final_label` (scenario) | `quotes_regime_earnings_v1` | `train_event_reaction_scenario_classifier.py` | Fusion / shadow |

Подробнее: [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) (§сводная матрица, §10 complementarity, §11 единая точка решения).

---

## 4. Фазы: единый словарь

Два набора терминов **дополняют** друг друга:

### 4.1 Калибровка модели (ML_CALIBRATION A–E)

| Фаза | Смысл |
|------|--------|
| **A** | Целостность данных и признаков |
| **B** | Стабильность модели (гиперпараметры, dry-run train) |
| **C** | Честная OOS-оценка (analyzer, walk-forward) |
| **D** | Политика исполнения (fusion, пороги, gate modes) |
| **E** | Прод + мониторинг, continuous retrain |

### 4.2 Жизненный цикл контура (ML_UNIFIED)

| Фаза | Смысл | Типичное соответствие A–E |
|------|--------|---------------------------|
| `accumulating_data` | Label/build, train skip или dry-run | A |
| `quality_tuning` | Incremental train при Δ данных | A–B, начало C |
| `product_ready` | Gates + shadow OK | C–D |
| `continuous_prod` | Train на каждый apply после product gate | E |

---

## 5. Cron: каноническое расписание

**Источник правды:** `crontab/lse-docker.crontab` (при расхождении с markdown — верить crontab).

### 5.1 L1 — data-driven poll

| Cron | Скрипт | Контуры |
|------|--------|---------|
| `15 */6` | `run_ml_refresh_dispatcher.py` | `open_path`, `earnings_grid`, `game5m_entry`, `portfolio`, `event_reaction_regression` |

Train внутри скриптов **только по триггеру** (кроме явного `--full`).

### 5.2 L1 — nightly data prep (общие факты)

| MSK пн–пт | Скрипт | Назначение |
|-----------|--------|------------|
| 23:32 | `ingest_market_regime_daily.py` | режим рынка |
| 23:33–37 | ERD build + backfill | skeleton + labels + earnings_v1 features |
| 23:45 | `label_open_path_scenarios.py` | open-path labels |

### 5.3 L1 — nightly refresh (`--slot nightly`)

| MSK пн–пт | Скрипт | Контуры (порядок) |
|-----------|--------|-------------------|
| 23:40 | `run_daily_game5m_ml_pipeline.py` | datasets only |
| 23:45 | `label_open_path_scenarios.py` | open-path labels |
| 23:47 | `run_ml_refresh_dispatcher.py --slot nightly` | game5m_entry → open_path → event_reaction → earnings_grid → portfolio → gap_forecast |
| вс 06:05 | `run_ml_refresh_dispatcher.py --slot weekly_full` | multiday_lr, recovery, gap_forecast, open_path, game5m_entry |

### 5.4 L2 — gates и отчёты

| MSK пн–пт | Скрипт |
|-----------|--------|
| 23:50 | `run_ml_train_readiness_cron.py` → JSONL + aggregate `ml_contours_status` |
| 23:53 | `run_ml_data_quality_report.py` |

### 5.5 GAME_5M ops (session + gap)

| Cron MSK | Скрипт | Назначение |
|----------|--------|------------|
| пн–пт 16:50 | `analyze_game5m_gap_forecast.py --days 90` | rolling PM vs ML → `last_gap_forecast_metrics.json` |
| пн–пт 23:35 | `cron_game5m_daily_session_review.sh` | post-RTH review + optional analyzer snapshot (1d) |
| вс 06:25 | `run_multiday_wf_game5m.py` | walk-forward multiday OOS |

### 5.6 Прочее (не ML train)

| Cron | Скрипт |
|------|--------|
| `15 */2` | `run_earnings_intelligence_autoprep.py` (materials, не train) |
| вс 23:48 | open-path `--full` shadow |
| вс 06:00 | `run_earnings_intelligence_prod_eval.py` |
| вс 06:10 | `report_decision_stack_mirror.py --days 14` | mirror telemetry (phase 3) |

Детали retrain-контракта: [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md).

---

## 6. Promotion playbook (L2 → L3)

Для каждого контура перед влиянием на сделки:

| Шаг | Действие | Проверка |
|-----|----------|----------|
| 1 | L1 train стабилен, артефакт `.cbm` / JSON на месте | `last_*_train_metrics.json` |
| 2 | L2 gate `ready=true` | `ml_train_readiness.jsonl` или `last_*_readiness.json` |
| 3 | L2 фаза C: analyzer OOS / shadow | `ml_production_arbiter`, shadow reports |
| 3b | **L2.5 Trust** (историческая правота) | [DECISION_TRUST_ARBITER.md](DECISION_TRUST_ARBITER.md) — post-mortem, `trust_score` |
| 4 | L3 `DECISION_STACK_READINESS_<CONTOUR>=production` (или override) | `decision_snapshot` |
| 5 | L3 gate mode `apply` | `DECISION_STACK_*_GATE_MODE=apply` |
| 6 | Runtime flag | `*_ENABLED=true` |
| 7 | Mirror-телеметрия | `report_decision_stack_mirror.py`, `decision_snapshot` в сделках |
| 8 | `DECISION_STACK_RESOLVE_ENABLED=true` | **опционально**, ручной toggle после ops sign-off |

**Сейчас на prod:** `DECISION_STACK_RESOLVE_ENABLED=false` — legacy исполняет; stack в shadow. Session-divergence (NEAR_OPEN/CLOSE) принята как норма, пока статистика за legacy ([ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md) §3.0).

---

## 7. Артефакты (единый каталог)

База: `/app/logs/ml/ml_data_quality/` (в dev: `local/logs/ml_data_quality/`).

| Файл | Слой |
|------|------|
| `ml_contours_status.json` | L1 aggregate |
| `ml_train_readiness.jsonl` | L2 CatBoost + event |
| `last_earnings_intelligence_readiness.json` | L2 earnings |
| `last_open_path_readiness.json` | L2 open-path |
| `report_daily.json` | L2 snapshot для API |
| `last_<contour>_ml_refresh.json` | L1 per-contour |
| `last_<contour>_train_metrics.json` | L1/L2 metrics |

UI: `/analyzer` → «Переобучение ML по контурам» (L1), таблица гейтов (L2), `ml_production_arbiter` (C).

---

## 8. Индекс документов (tiered)

### Канон (читать первым)

| Документ | Тема |
|----------|------|
| **Этот файл** | Контуры, слои, поверхности, cron, promotion |
| [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) | **Словарь:** L1/L2/L3, метрики, BMO/AMH, open-path vs event 5d, примеры |
| [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md) | План устранения дублей и code gaps |
| [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md) | Живой ops-статус (что сделано / висит) |
| [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) | Алгоритм решений GAME_5M |

### Deep-dive (детали, не дублировать канон)

| Документ | Тема |
|----------|------|
| [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md) | L1 контракт, триггеры, pseudocode |
| [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) | Фазы A–E по сеткам |
| [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md) | L2 JSONL, API `/api/ml/data-quality` |
| [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) | Датасеты, таргеты, метрики; §0 event 5d vs open-path |
| [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md) | ERD backfill, якоря BMO/AMH, vol-scaled labels |
| [DECISION_STACK_ROLLOUT_PLAN.md](DECISION_STACK_ROLLOUT_PLAN.md) | Имплементация L3 (фазы 0–14) |
| [OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md](OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md) | Earnings autoprep + open-path gates |
| [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md) | Earnings фазы 1–5 |
| [earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md](earnings-event-agent-lse/EARNINGS_PRODUCT_ROADMAP.md) | Earnings product maturity |

### Архив (исторические снимки)

| Документ | Заменён на |
|----------|------------|
| [WEEKLY_PLAN_GAME5M_AND_ML_2026-05-06.md](archive/WEEKLY_PLAN_GAME5M_AND_ML_2026-05-06.md) | PROJECT_STATUS + этот канон |
| `earnings-event-agent-lse/EARNINGS_PLAN_2026-05-*.md` | IMPLEMENTATION_PLAN + PRODUCT_ROADMAP |
| [earnings-event-agent-lse/EARNINGS_INTELLIGENCE_PLAN.md](archive/EARNINGS_INTELLIGENCE_PLAN.md) | PRODUCT_ROADMAP + IMPLEMENTATION_PLAN |

Rollout-планы с пометкой **Superseded for architecture** остаются полезны для **имплементации конкретных фич** (multiday gates, analyzer contours), но матрица контуров и cron — только здесь и в `ML_UNIFIED`.

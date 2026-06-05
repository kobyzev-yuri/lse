# Документация LSE — навигация

Корневой обзор архитектуры: [ARCHITECTURE.md](ARCHITECTURE.md).

## ML и торговые решения (читать первым)

| Задача | Документ |
|--------|----------|
| **Канон:** контуры, слои L1–L3, cron, promotion | [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md) |
| **План консолидации** (устранение дублей, фазы 0–4) | [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md) |
| **Ops-статус** (сделано / висит) | [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md) |
| L1 retrain (триггеры, registry) | [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md) |
| L2 gates, data-quality API | [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md) |
| L3 GAME_5M алгоритм | [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) |
| Датасеты и таргеты всех сеток | [TRADE_ML_DATASETS_AND_TARGETS_RU.md](TRADE_ML_DATASETS_AND_TARGETS_RU.md) |

## Портфельная игра

| Задача | Документ |
|--------|----------|
| Алгоритм, стратегии, вход/выход, справочник `PORTFOLIO_*` | [PORTFOLIO_GAME.md](PORTFOLIO_GAME.md) |
| CatBoost (карточки, фильтр входа, ML-тейк, trailing) | [ML_PORTFOLIO_CATBOOST.md](ML_PORTFOLIO_CATBOOST.md) |
| Крон vs GAME_5M, тейк/стоп в логах | [CRONS_AND_TAKE_STOP.md](CRONS_AND_TAKE_STOP.md) |
| Ключи config.env | [CONFIG_OPTIONS_ANALYSIS.md](CONFIG_OPTIONS_ANALYSIS.md) §6 |
| Бизнес-процессы | [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md) |
| Multiday ridge | [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md), [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) |

## Игра GAME_5M (одна цепочка: вход → удержание → выход → разбор)

| Задача | Документ |
|--------|----------|
| **Каноническая архитектура решения**: rules + observable baselines + ML readiness/gates + premarket gap | [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) |
| Основной цикл крона, тейк/стоп, **вход в начале RTH** (премаркет, NEAR_OPEN, импульс) | [GAME_5M_CALCULATIONS_AND_REPORTING.md](GAME_5M_CALCULATIONS_AND_REPORTING.md) |
| **Макро VIX / Forex / нефть** (entry_advice, карточки, премаркет, порядок внедрения) | [GAME_5M_MACRO_RISK.md](GAME_5M_MACRO_RISK.md) |
| **Песочница идей + арбитр** (вердикт по макро в анализаторе) | [GAME_5M_PRODUCT_IDEAS_ARBITER.md](GAME_5M_PRODUCT_IDEAS_ARBITER.md) |
| Премаркет: цена, импульс в первые минуты | [GAME_5M_PREMARKET_AND_IMPULSE.md](GAME_5M_PREMARKET_AND_IMPULSE.md) |
| **Пайплайн** висяки / упущенная выгода / датасеты / анализатор | [GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md](GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md) (раздел «Пайплайн») |
| JSON сделки, `context_json` | [GAME_5M_DEAL_PARAMS_JSON.md](GAME_5M_DEAL_PARAMS_JSON.md) |
| CatBoost на входе, fusion, **метрики после каждого обучения** | [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md), [GAME_5M_CATBOOST_FUSION.md](GAME_5M_CATBOOST_FUSION.md) |
| **Multiday ridge** (лог-доходность 1–3 торг. дня, калибровка, скрипт train) | [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md), [ML_CALIBRATION_PHASES.md](ML_CALIBRATION_PHASES.md) |
| **Decision stack rollout** (механика snapshot/gates; концепция решения вынесена выше) | [DECISION_STACK_ROLLOUT_PLAN.md](DECISION_STACK_ROLLOUT_PLAN.md) |
| Отчёт анализатора, LLM, снимки, автотюн | [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md) |
| Регламент tuning + **replay proposals** (график, ledger, apply) | [GAME_5M_TUNING_REGLEMENT.md](GAME_5M_TUNING_REGLEMENT.md) |
| Кроны, деплой | [CRONS_AND_TAKE_STOP.md](CRONS_AND_TAKE_STOP.md), [RUN_GAME_SERVICES.md](RUN_GAME_SERVICES.md) |

## Скрипты конвейера (кратко)

| Скрипт | Назначение |
|--------|------------|
| `scripts/run_daily_game5m_ml_pipeline.py` | После сессии: stuck + continuation CSV + JSONL (train — через dispatcher / `run_game5m_entry_ml_refresh`) |
| `scripts/run_ml_refresh_dispatcher.py` | Poll `*/6` или `--slot nightly` / `weekly_full` — все 8 ML contours |
| `scripts/run_multiday_lr_ml_refresh.py` | Multiday ridge JSON refit (trigger / weekly_full) |
| `scripts/run_recovery_ml_refresh.py` | Recovery export JSONL + CatBoost train |
| `scripts/run_gap_forecast_refresh.py` | Gap forecast metrics + optional OLS coef suggestions |
| `scripts/build_game5m_stuck_dataset.py` | Датасет риска зависания |
| `scripts/build_game5m_continuation_dataset.py` | Датасет underprofit / continuation |
| `scripts/train_game5m_catboost.py` | Обучение entry-модели; **`--json-metrics-out`** для машинного снимка метрик |
| `scripts/run_ml_data_quality_report.py` | **Единый отчёт** БД / event_analytics / датасеты / CatBoost + опц. dry-run train + **LLM** |
| `scripts/migrate_ml_event_analytics.py` | DDL: `event_reaction_dataset`, `earnings_event_detail`, … |
| `scripts/train_event_reaction_catboost.py` | Event/earnings MVP: `event_reaction_dataset` → forward 5d log-ret; **`--json-metrics-out`** |
| `scripts/build_event_reaction_dataset.py` | Skeleton строк датасета из KB (`--from-kb-earnings`) |
| `scripts/backfill_event_reaction_labeling.py` | Авторазметка `event_reaction_dataset` из daily `quotes` (MVP) |
| `scripts/seed_quotes_for_event_reaction_dataset.py` | Догрузка `quotes` по тикерам из датасета (если `no_quotes`) |
| `scripts/run_ml_train_readiness_cron.py` | Регулярные метрики + гейты готовности → `ml_train_readiness.jsonl` |
| `scripts/train_game5m_multiday_lr.py` | Ridge по дневным close (1–3 торг. дня); `--tickers-source`, **`--json-metrics-out`**, `--dry-run` — см. [GAME_5M_MULTIDAY_LR_RIDGE.md](GAME_5M_MULTIDAY_LR_RIDGE.md) |
| `scripts/snapshot_analyzer_report.py` | Снимок JSON анализатора для офлайна / cron |

## Earnings / event agent (дизайн)

| Материал | Ссылка |
|----------|--------|
| **План внедрения 1–5 + источники (рабочая версия)** | [EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md) |
| **IR и квартальная отчётность:** дефолтные акции из `TICKER_GROUPS` + ASML, ARM, примеры кварталов | [PUBLIC_IR_EARNINGS_SOURCES.md](earnings-event-agent-lse/PUBLIC_IR_EARNINGS_SOURCES.md) |
| Earnings/event agent: дизайн, **Q&amp;A в Markdown** (удобно на GitHub), PDF/HTML | [earnings-event-agent-lse/](earnings-event-agent-lse/README.md) |

# Документация LSE — навигация

Корневой обзор архитектуры: [ARCHITECTURE.md](ARCHITECTURE.md). Длинные бизнес-процессы: [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md).

## Игра GAME_5M (одна цепочка: вход → удержание → выход → разбор)

| Задача | Документ |
|--------|----------|
| Основной цикл крона, тейк/стоп, **вход в начале RTH** (премаркет, NEAR_OPEN, импульс) | [GAME_5M_CALCULATIONS_AND_REPORTING.md](GAME_5M_CALCULATIONS_AND_REPORTING.md) |
| Премаркет: цена, импульс в первые минуты | [GAME_5M_PREMARKET_AND_IMPULSE.md](GAME_5M_PREMARKET_AND_IMPULSE.md) |
| **Пайплайн** висяки / упущенная выгода / датасеты / анализатор | [GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md](GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md) (раздел «Пайплайн») |
| JSON сделки, `context_json` | [GAME_5M_DEAL_PARAMS_JSON.md](GAME_5M_DEAL_PARAMS_JSON.md) |
| CatBoost на входе, fusion, **метрики после каждого обучения** | [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md), [GAME_5M_CATBOOST_FUSION.md](GAME_5M_CATBOOST_FUSION.md) |
| Отчёт анализатора, LLM, снимки, автотюн | [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md) |
| Регламент tuning + **replay proposals** (график, ledger, apply) | [GAME_5M_TUNING_REGLEMENT.md](GAME_5M_TUNING_REGLEMENT.md) |
| Кроны, деплой | [CRONS_AND_TAKE_STOP.md](CRONS_AND_TAKE_STOP.md), [RUN_GAME_SERVICES.md](RUN_GAME_SERVICES.md) |

## Скрипты конвейера (кратко)

| Скрипт | Назначение |
|--------|------------|
| `scripts/run_daily_game5m_ml_pipeline.py` | После сессии: stuck CSV + continuation CSV + CatBoost + строка в `game5m_daily_ml_report.jsonl` |
| `scripts/build_game5m_stuck_dataset.py` | Датасет риска зависания |
| `scripts/build_game5m_continuation_dataset.py` | Датасет underprofit / continuation |
| `scripts/train_game5m_catboost.py` | Обучение entry-модели |
| `scripts/snapshot_analyzer_report.py` | Снимок JSON анализатора для офлайна / cron |

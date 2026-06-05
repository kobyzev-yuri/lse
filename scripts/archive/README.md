# Архив скриптов

Устаревшие, заменённые или разовые скрипты. **Не используйте в cron** — эталон prod: `crontab/lse-docker.crontab` + `run_ml_refresh_dispatcher.py`.

**Актуальная ML-оркестрация:** [docs/ML_AND_DECISION_ARCHITECTURE.md](../../docs/ML_AND_DECISION_ARCHITECTURE.md).

## `ml/` — заменены консолидацией L1

| Файл | Вместо |
|------|--------|
| `run_daily_game5m_recovery_pipeline.py` | `run_recovery_ml_refresh.py` (dispatcher `--slot weekly_full`) |
| `daily_game5m_ml_after_close.sh` | `run_daily_game5m_ml_pipeline.py` (cron 23:40, datasets only) |
| `train_premarket_gap_model.py` | `ingest_game5m_gap_forecast.py` + `run_gap_forecast_refresh.py` |

## `incidents/` — разовые ops / расследования

Скрипты для конкретных инцидентов (LITE, 2026-05-05, ручные новости, правки trade_history). Запуск только вручную при необходимости.

Stub-файлы в `scripts/` перенаправляют сюда для обратной совместимости путей в документации.

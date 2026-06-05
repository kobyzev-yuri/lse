#!/usr/bin/env bash
# Обертка для cron на хосте: сбор датасетов GAME_5M + обучение CatBoost + JSONL-отчёт.
# См. scripts/run_daily_game5m_ml_pipeline.py (переменные DAILY_ML_*).
#
# На сервере с docker-compose (сервис lse → контейнер lse-bot, /app внутри образа):
#
#   cd /path/to/lse   # каталог с docker-compose.yml
#   docker compose exec lse bash -lc '/app/scripts/daily_game5m_ml_after_close.sh'
#
# Или одной строкой без этой обёртки:
#   docker compose exec -T lse python3 scripts/run_daily_game5m_ml_pipeline.py
#
# Лог в примонтированный volume:
#   docker compose exec lse bash -lc 'python3 scripts/run_daily_game5m_ml_pipeline.py >> /app/logs/game5m_daily_ml_pipeline.log 2>&1'
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 "$ROOT/scripts/run_daily_game5m_ml_pipeline.py" "$@"

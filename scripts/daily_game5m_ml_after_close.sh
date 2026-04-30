#!/usr/bin/env bash
# Обертка для cron: сбор датасетов GAME_5M + обучение CatBoost + JSONL-отчёт.
# См. scripts/run_daily_game5m_ml_pipeline.py (переменные DAILY_ML_*).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec python3 "$ROOT/scripts/run_daily_game5m_ml_pipeline.py" "$@"

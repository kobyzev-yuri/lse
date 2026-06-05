#!/usr/bin/env bash
# Full ML train: all 8 contours, multiday on merged tickers (quotes ∪ config).
# Run inside lse-bot container or via: docker exec lse-bot bash /app/scripts/run_ml_full_train_all.sh
set -euo pipefail

export ML_READINESS_TRAIN_MODE="${ML_READINESS_TRAIN_MODE:-full}"
export ML_MULTIDAY_LR_TICKERS_SOURCE="${ML_MULTIDAY_LR_TICKERS_SOURCE:-merged}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"

echo "=== ML full train: ML_READINESS_TRAIN_MODE=$ML_READINESS_TRAIN_MODE ML_MULTIDAY_LR_TICKERS_SOURCE=$ML_MULTIDAY_LR_TICKERS_SOURCE ==="

echo "=== 1/2 game5m datasets (no catboost train) ==="
$PY scripts/run_daily_game5m_ml_pipeline.py

echo "=== 2/2 all contours --force-full ==="
$PY scripts/run_ml_refresh_dispatcher.py --force-full

echo "=== readiness snapshot ==="
$PY scripts/run_ml_train_readiness_cron.py || true

echo "=== done ==="

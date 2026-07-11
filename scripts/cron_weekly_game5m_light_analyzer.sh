#!/usr/bin/env bash
# Weekly GAME_5M light analyzer (no 5m OHLC — avoids OOM in lse-bot).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "${ROOT}/logs"
echo "=== $(date -Is) weekly_game5m_light_analyzer ==="
docker exec lse-bot python3 /app/scripts/run_game5m_light_analyzer.py \
  --days 7 \
  --json-out /app/logs/ml/ml_data_quality/analyzer_7d_light.json

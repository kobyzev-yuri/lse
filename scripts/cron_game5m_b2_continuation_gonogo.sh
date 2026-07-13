#!/usr/bin/env bash
# B2 continuation go/no-go review (список B). See docs/GAME_5M_B_LIST_RUN_SCHEDULE.md
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "${ROOT}/logs"
echo "=== $(date -Is) game5m_b2_continuation_gonogo ==="
docker exec lse-bot python3 /app/scripts/run_game5m_b2_continuation_gonogo_review.py \
  --days 30 \
  --json-out /app/logs/ml/ml_data_quality/last_game5m_b2_continuation_gonogo.json \
  >> "${ROOT}/logs/game5m_b2_continuation_gonogo.log" 2>&1

#!/usr/bin/env bash
# Weekly GAME_5M tactic scorecard (bundle + hold-to-gap + experiment observe).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "${ROOT}/logs"
echo "=== $(date -Is) weekly_game5m_tactic_review ==="
docker exec lse-bot python3 /app/scripts/weekly_game5m_tactic_review.py \
  --days 7 \
  --json-out /app/logs/ml/ml_data_quality/last_weekly_game5m_tactic_review.json

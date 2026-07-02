#!/usr/bin/env bash
# Post-RTH GAME_5M daily review (run on GCP host after US close, ~23:30 MSK weekdays).
# Crontab example:
#   30 23 * * 1-5 /home/ai8049520/lse/scripts/cron_game5m_daily_session_review.sh >> /home/ai8049520/lse/logs/game5m_daily_review.log 2>&1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
echo "=== $(date -Is) game5m_daily_session_review ==="
docker exec lse-bot python3 /app/scripts/game5m_daily_session_review.py
echo "=== $(date -Is) game5m_trade_postmortem ==="
docker exec lse-bot python3 /app/scripts/game5m_trade_postmortem.py --window-days 14
# Lightweight analyzer snapshot (1 day — avoids 3d timeout on prod)
if docker exec lse-bot test -f /app/scripts/snapshot_analyzer_report.py 2>/dev/null; then
  docker exec -e ANALYZER_SNAPSHOT_URL=http://127.0.0.1:8080/api/analyzer lse-bot \
    python3 /app/scripts/snapshot_analyzer_report.py --days 1 --strategy GAME_5M \
      --out-dir /app/logs/ml/ml_data_quality --no-trade-details --quiet 2>/dev/null || true
fi

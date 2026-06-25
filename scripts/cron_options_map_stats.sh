#!/usr/bin/env bash
# PCR volume stats from options_chain_oi_snapshot → last_options_map_cron_stats.json
# (калибровка порогов Money Map: p25/p75 vs wireframe 0.87/1.15).
# Cron example (UTC, Mon–Fri 23:45, после snapshot OI 23:30):
#   45 23 * * 1-5 flock -n /tmp/lse_options_map_stats.lock /home/ai8049520/lse/scripts/cron_options_map_stats.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${LSE_CONTAINER:-lse-bot}"
LOG="${LSE_HOME:-$ROOT}/logs/options_map_cron_stats.log"
mkdir -p "$(dirname "$LOG")"
exec docker exec "$CONTAINER" python scripts/analyze_options_map_cron_stats.py \
  --days 90 \
  --json-out /app/logs/ml/ml_data_quality/last_options_map_cron_stats.json \
  >>"$LOG" 2>&1

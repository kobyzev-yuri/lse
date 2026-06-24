#!/usr/bin/env bash
# Daily Polygon option OI snapshot → options_chain_oi_snapshot (Money Map history).
# Cron example (UTC, Mon–Fri 23:30):
#   30 23 * * 1-5 flock -n /tmp/lse_options_oi_snap.lock /home/ai8049520/lse/scripts/cron_options_chain_oi.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${LSE_CONTAINER:-lse-bot}"
LOG="${LSE_HOME:-$ROOT}/logs/options_oi_snapshot.log"
mkdir -p "$(dirname "$LOG")"
exec docker exec "$CONTAINER" python scripts/snapshot_options_chain_oi.py "$@" >>"$LOG" 2>&1

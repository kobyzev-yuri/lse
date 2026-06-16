#!/usr/bin/env bash
# GAME_5M parameter tuning cron wrapper (propose weekly / observe daily).
# Usage on host:
#   ./scripts/cron_game5m_tuning.sh propose
#   ./scripts/cron_game5m_tuning.sh observe
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-observe}"
mkdir -p "${ROOT}/local" "${ROOT}/logs"
echo "=== $(date -Is) game5m_tuning ${MODE} ==="
case "${MODE}" in
  propose)
    docker exec lse-bot python3 /app/scripts/run_game5m_tuning_cycle.py propose \
      --days 30 --max-trades 40 --top-n 8 --horizon-tail-days 1 \
      --families exit
    ;;
  observe)
    docker exec lse-bot python3 /app/scripts/run_game5m_tuning_cycle.py observe \
      --min-new-trades 8
    ;;
  *)
    echo "Unknown mode: ${MODE} (use propose|observe)" >&2
    exit 2
    ;;
esac

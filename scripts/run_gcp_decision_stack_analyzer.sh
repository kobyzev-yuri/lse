#!/usr/bin/env bash
# Run the production analyzer on gcp-lse and print a compact decision_stack readiness summary.
#
# Defaults are tuned for "after each game" review:
#   DAYS=7 STRATEGY=ALL USE_LLM=1 ./scripts/run_gcp_decision_stack_analyzer.sh
#
# Useful overrides:
#   DAYS=3 STRATEGY=GAME_5M USE_LLM=0 ./scripts/run_gcp_decision_stack_analyzer.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SSH_HOST="${SSH_HOST:-gcp-lse}"
SSH_CONFIG="${SSH_CONFIG:-/home/cnn/.ssh/config}"
SSH_IDENTITY="${SSH_IDENTITY:-/home/cnn/.ssh/1234}"
CONTAINER="${CONTAINER:-lse-bot}"
DAYS="${DAYS:-7}"
STRATEGY="${STRATEGY:-ALL}"
USE_LLM="${USE_LLM:-1}"
OUT_DIR="${OUT_DIR:-local/analyzer_snapshots}"

mkdir -p "$OUT_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
remote_json="/tmp/lse_analyzer_${STRATEGY}_llm${USE_LLM}_${DAYS}d_${ts}.json"
local_json="${OUT_DIR}/gcp_lse_analyzer_${STRATEGY}_llm${USE_LLM}_${DAYS}d_${ts}.json"
latest_json="${OUT_DIR}/gcp_lse_analyzer_${STRATEGY}_llm${USE_LLM}_${DAYS}d_latest.json"

llm_flag="--no-use-llm"
if [[ "$USE_LLM" == "1" || "$USE_LLM" == "true" || "$USE_LLM" == "yes" ]]; then
  llm_flag="--use-llm"
fi

ssh_base=(ssh -F "$SSH_CONFIG" -i "$SSH_IDENTITY" -o BatchMode=yes "$SSH_HOST")
scp_base=(scp -F "$SSH_CONFIG" -i "$SSH_IDENTITY")

echo "[analyzer] running on ${SSH_HOST}:${CONTAINER} strategy=${STRATEGY} days=${DAYS} llm=${USE_LLM}" >&2
"${ssh_base[@]}" \
  "docker exec ${CONTAINER} bash -lc 'cd /app && python3 scripts/export_analyzer_report.py --days ${DAYS} --strategy ${STRATEGY} ${llm_flag} --no-include-trade-details' > '${remote_json}'"

"${scp_base[@]}" "${SSH_HOST}:${remote_json}" "$local_json" >/dev/null
cp "$local_json" "$latest_json"

echo "[analyzer] saved: $local_json" >&2
echo "[analyzer] latest: $latest_json" >&2

python3 scripts/summarize_decision_stack_readiness.py "$latest_json"

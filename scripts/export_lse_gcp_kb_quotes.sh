#!/usr/bin/env bash
# Выгрузка knowledge_base (без embedding) и quotes с GCP VM в tradenews/datasets/lse_gcp_dump/
#
# Использование (из корня репозитория lse):
#   ./scripts/export_lse_gcp_kb_quotes.sh
#   DAYS=30 SSH_TARGET=gcp-lse ./scripts/export_lse_gcp_kb_quotes.sh
# SSH: см. docs/GCP_LSE_SSH.md (Host gcp-lse, IdentityFile ~/.ssh/1234)

set -euo pipefail

SSH_TARGET="${SSH_TARGET:-gcp-lse}"
DAYS="${DAYS:-90}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/tradenews/datasets/lse_gcp_dump}"
STAMP="$(date -u +%Y%m%d_%H%M%SZ)"

mkdir -p "$OUT_DIR"
KB_CSV="$OUT_DIR/knowledge_base_last${DAYS}d_${STAMP}.csv"
QT_CSV="$OUT_DIR/quotes_last${DAYS}d_${STAMP}.csv"
META="$OUT_DIR/README_IMPORT.txt"

echo "SSH_TARGET=$SSH_TARGET DAYS=$DAYS -> $OUT_DIR"

run_remote_copy() {
  local sql_copy=$1
  local out=$2
  printf '%s\n' "$sql_copy" | ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$SSH_TARGET" \
    "docker exec -i lse-postgres psql -U postgres -d lse_trading -v ON_ERROR_STOP=1" \
    >"$out"
}

echo "Exporting knowledge_base (no embedding)..."
KB_SQL="\\copy ( SELECT id, ts, ticker, source, content, sentiment_score, event_type, region, importance, link, insight, COALESCE(outcome_json::text, '') AS outcome_json_text, ingested_at FROM knowledge_base WHERE ts >= NOW() - interval '${DAYS} days' ORDER BY ts, id ) TO STDOUT WITH CSV HEADER"
run_remote_copy "$KB_SQL" "$KB_CSV"

echo "Exporting quotes..."
QT_SQL="\\copy ( SELECT id, date, ticker, \"open\", high, low, close, volume, sma_5, volatility_5, rsi, macd, macd_signal, macd_hist, bbands_upper, bbands_middle, bbands_lower, adx, stoch_k, stoch_d FROM quotes WHERE date >= NOW() - interval '${DAYS} days' ORDER BY ticker, date, id ) TO STDOUT WITH CSV HEADER"
run_remote_copy "$QT_SQL" "$QT_CSV"

wc -l "$KB_CSV" "$QT_CSV" | tee "$META"
{
  echo "exported_utc=$STAMP"
  echo "days_window=$DAYS"
  echo "ssh_target=$SSH_TARGET"
  echo "knowledge_base_csv=$(basename "$KB_CSV")"
  echo "quotes_csv=$(basename "$QT_CSV")"
} >>"$META"

echo "Done."

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

precheck() {
  local out rc
  echo "Checking SSH -> $SSH_TARGET (ConnectTimeout=25s)..."
  set +e
  out=$(ssh -o BatchMode=yes -o ConnectTimeout=25 -o ConnectionAttempts=1 -o StrictHostKeyChecking=no \
    "$SSH_TARGET" "echo _ssh_ok" 2>&1)
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]] || [[ "$out" != *"_ssh_ok"* ]]; then
    echo "FAIL: SSH к $SSH_TARGET не установлен (код $rc)." >&2
    echo "$out" >&2
    echo "" >&2
    echo "Это не про Docker: до сервера не достучались (таймаут, firewall, другой IP, VM выключена)." >&2
    echo "Проверьте: внешний IP в GCP, правило firewall tcp/22, ssh -v $SSH_TARGET" >&2
    exit 1
  fi
  echo "OK: SSH отвечает."
  echo "Checking Postgres container (name contains lse-postgres)..."
  set +e
  ssh -o BatchMode=yes -o ConnectTimeout=25 -o StrictHostKeyChecking=no "$SSH_TARGET" \
    "docker ps --format '{{.Names}}' | grep -q 'lse-postgres'" >/dev/null 2>&1
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo "FAIL: SSH есть, но контейнер с lse-postgres в имени не найден." >&2
    echo "Список контейнеров на VM:" >&2
    ssh -o BatchMode=yes -o ConnectTimeout=25 -o StrictHostKeyChecking=no "$SSH_TARGET" \
      "docker ps --format 'table {{.Names}}\t{{.Status}}'" >&2 || true
    exit 1
  fi
  echo "OK: контейнер Postgres запущен."
}

remote_scalar() {
  # Одна ячейка результата (COUNT и т.д.)
  local sql=$1
  local q
  q=$(printf '%q' "$sql")
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$SSH_TARGET" \
    "docker exec lse-postgres psql -U postgres -d lse_trading -t -A -q -v ON_ERROR_STOP=1 -c $q" | tr -d ' \n\r' || true
}

run_remote_copy() {
  local sql_copy=$1
  local out=$2
  local label=$3
  local tmp err rc _sz
  tmp="${out}.part.$$"
  err="${out}.stderr.log"
  rm -f "$out" "$tmp" "$err"
  echo "Streaming $label -> $(basename "$out") (временный файл: $(basename "$tmp"))"
  echo "  (пока идёт поток CSV, в этом терминале тишина — это норма; большие выгрузки — минуты.)"
  echo "  В другом терминале: watch -n2 ls -lh \"$tmp\""
  set +e
  printf '%s\n' "$sql_copy" | ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$SSH_TARGET" \
    "docker exec -i lse-postgres psql -U postgres -d lse_trading -v ON_ERROR_STOP=1" \
    2>"$err" >"$tmp"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    echo "FAIL: $label — ssh/psql завершились с кодом $rc" >&2
    echo "  stderr сохранён: $err" >&2
    [[ -s "$err" ]] && cat "$err" >&2
    rm -f "$tmp"
    exit "$rc"
  fi
  _sz=$(wc -c <"$tmp" | tr -d '[:space:]' || echo 0)
  # COPY CSV HEADER даёт хотя бы строку заголовка; 0 байт = сбой без данных
  if [[ "${_sz:-0}" -lt 30 ]]; then
    echo "FAIL: $label — слишком мало данных (${_sz} байт), целевой CSV не создан." >&2
    echo "  Смотрите: $err и первые байты потока:" >&2
    [[ -s "$err" ]] && cat "$err" >&2
    head -c 400 "$tmp" 2>/dev/null | cat -v >&2 || true
    rm -f "$tmp"
    exit 1
  fi
  if ! head -1 "$tmp" | grep -qi 'ticker'; then
    echo "FAIL: $label — в первой строке нет ожидаемого CSV header (колонка ticker)." >&2
    echo "  Первые 200 символов:" >&2
    head -c 200 "$tmp" | cat -v >&2
    echo >&2
    rm -f "$tmp"
    exit 1
  fi
  mv -f "$tmp" "$out"
  rm -f "$err"
  if command -v numfmt >/dev/null 2>&1; then
    echo "  готово: $(numfmt --to=iec-i --suffix=B <<<"$_sz") -> $(basename "$out")"
  else
    echo "  готово: ${_sz} байт -> $(basename "$out")"
  fi
}

precheck

echo "Row counts (approx., quick):"
KB_N=$(remote_scalar "SELECT count(*) FROM knowledge_base WHERE ts >= NOW() - interval '${DAYS} days';")
QT_N=$(remote_scalar "SELECT count(*) FROM quotes WHERE date >= NOW() - interval '${DAYS} days';")
echo "  knowledge_base rows in window: ${KB_N:-?}"
echo "  quotes rows in window: ${QT_N:-?}"

echo "Exporting knowledge_base (no embedding)..."
KB_SQL="\\copy ( SELECT id, ts, ticker, source, content, sentiment_score, event_type, region, importance, link, insight, COALESCE(outcome_json::text, '') AS outcome_json_text, ingested_at FROM knowledge_base WHERE ts >= NOW() - interval '${DAYS} days' ORDER BY ts, id ) TO STDOUT WITH CSV HEADER"
run_remote_copy "$KB_SQL" "$KB_CSV" "knowledge_base"

echo "Exporting quotes..."
QT_SQL="\\copy ( SELECT id, date, ticker, \"open\", high, low, close, volume, sma_5, volatility_5, rsi, macd, macd_signal, macd_hist, bbands_upper, bbands_middle, bbands_lower, adx, stoch_k, stoch_d FROM quotes WHERE date >= NOW() - interval '${DAYS} days' ORDER BY ticker, date, id ) TO STDOUT WITH CSV HEADER"
run_remote_copy "$QT_SQL" "$QT_CSV" "quotes"

wc -l "$KB_CSV" "$QT_CSV" | tee "$META"
{
  echo "exported_utc=$STAMP"
  echo "days_window=$DAYS"
  echo "ssh_target=$SSH_TARGET"
  echo "knowledge_base_csv=$(basename "$KB_CSV")"
  echo "quotes_csv=$(basename "$QT_CSV")"
} >>"$META"

echo "Done."

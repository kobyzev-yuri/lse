#!/usr/bin/env bash
# Срез по БД: влияние GAME_5M_TAKE_PROFIT_MIN_PCT (и похожих порогов) на записанный take_profit_pct при входе.
# Запускайте после торговой сессии, когда в trade_history уже есть свежие BUY/SELL.
#
# Использование:
#   ./scripts/run_game5m_take_profit_min_impact_after_session.sh
#   GAME5M_SSH=ai8049520@104.154.205.58 CUTOFF_MSK_DATE=2026-04-29 ./scripts/run_game5m_take_profit_min_impact_after_session.sh
#
# Переменные:
#   GAME5M_SSH          — SSH target (default: ai8049520@104.154.205.58)
#   PG_CONTAINER        — имя контейнера Postgres (default: lse-postgres)
#   PG_DB               — имя БД (default: lse_trading)
#   PG_USER             — пользователь Postgres (default: postgres)
#   SINCE_MSK_DATE      — начало выборки BUY, дата по Europe/Moscow (default: 14 дней назад от «сегодня» в MSK)
#   CUTOFF_MSK_DATE     — граница «до / после» смены min pct, полночь MSK (default: 2026-04-29)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SSH_TARGET="${GAME5M_SSH:-ai8049520@104.154.205.58}"
PG_CONTAINER="${PG_CONTAINER:-lse-postgres}"
PG_DB="${PG_DB:-lse_trading}"
PG_USER="${PG_USER:-postgres}"

# Дефолт SINCE: 14 дней назад (macOS: date -v; Linux: GNU date -d)
if test -z "${SINCE_MSK_DATE:-}"; then
  if date -v-14d +%F >/dev/null 2>&1; then
    SINCE_MSK_DATE="$(date -v-14d +%F)"
  else
    SINCE_MSK_DATE="$(date -d '14 days ago' +%F)"
  fi
fi

CUTOFF_MSK_DATE="${CUTOFF_MSK_DATE:-2026-04-29}"

echo "== GAME_5M take_profit_min impact (trade_history) =="
echo "SSH:              $SSH_TARGET"
echo "Postgres:         docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB"
echo "BUY since (MSK): $SINCE_MSK_DATE 00:00"
echo "Cutoff (MSK):    $CUTOFF_MSK_DATE 00:00 — before / from"
echo ""

ssh -o BatchMode=yes -o ConnectTimeout=20 "$SSH_TARGET" "docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1" <<SQL
\\echo '--- 1) BUY: импульс 2h в [2.5%, 3.0%) — сколько входов и средний take_profit_pct до/после cutoff (MSK)'
WITH b AS (
  SELECT id, ticker, ts,
    (context_json->>'momentum_2h_pct')::double precision AS mom,
    (context_json->>'take_profit_pct')::double precision AS tp
  FROM trade_history
  WHERE strategy_name = 'GAME_5M' AND side = 'BUY'
    AND ts >= (timestamptz '${SINCE_MSK_DATE} 00:00' AT TIME ZONE 'Europe/Moscow')
    AND context_json ? 'momentum_2h_pct'
)
SELECT
  CASE
    WHEN ts < (timestamptz '${CUTOFF_MSK_DATE} 00:00' AT TIME ZONE 'Europe/Moscow')
      THEN 'before_' || '${CUTOFF_MSK_DATE}'
    ELSE 'from_' || '${CUTOFF_MSK_DATE}'
  END AS period,
  count(*) FILTER (WHERE mom >= 2.5 AND mom < 3.0) AS buys_mom_25_30,
  round((avg(tp) FILTER (WHERE mom >= 2.5 AND mom < 3.0))::numeric, 4) AS avg_tp_in_band,
  min(tp) FILTER (WHERE mom >= 2.5 AND mom < 3.0) AS min_tp_in_band,
  max(tp) FILTER (WHERE mom >= 2.5 AND mom < 3.0) AS max_tp_in_band,
  count(*) AS buys_total
FROM b
GROUP BY 1
ORDER BY 1;

\\echo ''
\\echo '--- 2) BUY: все входы с mom в [2.5%, 3.0%) за период (детально)'
SELECT id, ticker, ts AT TIME ZONE 'Europe/Moscow' AS ts_msk,
  round((context_json->>'momentum_2h_pct')::numeric, 4) AS mom2h_pct,
  round((context_json->>'take_profit_pct')::numeric, 4) AS take_profit_pct
FROM trade_history
WHERE strategy_name = 'GAME_5M' AND side = 'BUY'
  AND ts >= (timestamptz '${SINCE_MSK_DATE} 00:00' AT TIME ZONE 'Europe/Moscow')
  AND (context_json->>'momentum_2h_pct')::float >= 2.5
  AND (context_json->>'momentum_2h_pct')::float < 3.0
ORDER BY id;

\\echo ''
\\echo '--- 3) SELL: количество по дням (MSK) и signal_type'
SELECT
  (date_trunc('day', ts AT TIME ZONE 'Europe/Moscow'))::date AS d_msk,
  signal_type,
  count(*) AS n
FROM trade_history
WHERE strategy_name = 'GAME_5M' AND side = 'SELL'
  AND ts >= (timestamptz '${SINCE_MSK_DATE} 00:00' AT TIME ZONE 'Europe/Moscow')
GROUP BY 1, 2
ORDER BY 1, 2;
SQL

echo ""
echo "Done."

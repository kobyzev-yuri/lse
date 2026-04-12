#!/usr/bin/env bash
# Применить миграции по порядку. Требуется psql.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [[ -n "${DATABASE_URL:-}" ]]; then
  export PGDATABASE=""
  PSQL=(psql "$DATABASE_URL" -v ON_ERROR_STOP=1)
else
  : "${PGDATABASE:?Set DATABASE_URL or PGDATABASE (and PGHOST/PGUSER/PGPASSWORD)}"
  PSQL=(psql -v ON_ERROR_STOP=1)
fi

for f in sql/001_extension_vector.sql \
         sql/010_knowledge_base_nyse.sql \
         sql/020_market_bars.sql \
         sql/030_news_signal_log.sql \
         sql/040_indexes.sql; do
  echo "# $f"
  "${PSQL[@]}" -f "$f"
done
echo "OK: all migrations applied."

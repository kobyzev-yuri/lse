#!/bin/bash
# Восстановление дампа PostgreSQL на новом сервере.
# Вариант 1: Postgres в Docker (docker-compose). Вариант 2: локальный Postgres.
# Запуск: ./scripts/restore_pg_dump.sh path/to/dump.sql.gz

set -e

DUMP_FILE="${1:?Укажите файл дампа: ./scripts/restore_pg_dump.sh lse_trading_dump_20260316.sql.gz}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -f "$DUMP_FILE" ]; then
  echo "❌ Файл не найден: $DUMP_FILE"
  exit 1
fi

# Если Postgres в Docker (контейнер lse-postgres)
if docker ps -q -f name=lse-postgres 2>/dev/null | head -1 | grep -q .; then
  echo "📥 Восстановление в контейнер lse-postgres..."
  CONTAINER=$(docker ps -q -f name=lse-postgres | head -1)
  gunzip -c "$DUMP_FILE" | docker exec -i "$CONTAINER" psql -U postgres -d lse_trading --set ON_ERROR_STOP=on
  echo "✅ Готово."
  exit 0
fi

# Локальный Postgres
if [ -n "$DATABASE_URL" ]; then
  echo "📥 Восстановление в БД из DATABASE_URL..."
  gunzip -c "$DUMP_FILE" | psql "$DATABASE_URL" --set ON_ERROR_STOP=on
  echo "✅ Готово."
  exit 0
fi

if [ -f config.env ]; then
  source <(grep -v '^#' config.env | grep -E '^DATABASE_URL=' | sed 's/^/export /')
  echo "📥 Восстановление в БД из config.env..."
  gunzip -c "$DUMP_FILE" | psql "$DATABASE_URL" --set ON_ERROR_STOP=on
  echo "✅ Готово."
  exit 0
fi

echo "❌ Запустите сначала docker-compose up -d postgres или задайте DATABASE_URL."
exit 1

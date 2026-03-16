#!/bin/bash
# Экспорт дампа PostgreSQL (lse_trading) для переноса на новый сервер.
# Запуск: на СТАРОМ сервере или с машины, где есть доступ к текущей БД.
# Требует: pg_dump, переменная DATABASE_URL или config.env в корне проекта.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -z "$DATABASE_URL" ] && [ -f config.env ]; then
  export DATABASE_URL=$(grep -E '^DATABASE_URL=' config.env | cut -d= -f2-)
fi
if [ -z "$DATABASE_URL" ]; then
  echo "❌ Задайте DATABASE_URL или создайте config.env с DATABASE_URL=postgresql://user:pass@host:5432/lse_trading"
  exit 1
fi
DUMP_URL="$DATABASE_URL"

OUTPUT="${1:-lse_trading_dump_$(date +%Y%m%d_%H%M%S).sql.gz}"
echo "📤 Экспорт в $OUTPUT (база из DATABASE_URL)..."
pg_dump "$DUMP_URL" --no-owner --no-acl --clean --if-exists | gzip > "$OUTPUT"
echo "✅ Готово: $(ls -lh "$OUTPUT")"
echo "   Перенесите файл на новый сервер и выполните restore (см. docs/MIGRATE_SERVER.md)."

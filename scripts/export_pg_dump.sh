#!/bin/bash
# Экспорт дампа PostgreSQL — только таблицы LSE (без чужих схем tbl_*, vanna_vectors и т.д.).
# Запуск: на СТАРОМ сервере или с машины, где есть доступ к текущей БД.
# Требует: pg_dump, переменная DATABASE_URL или config.env в корне проекта.

set -e

# Таблицы LSE (init_db.py): только они попадут в дамп
LSE_TABLES="quotes knowledge_base portfolio_state trade_history strategy_parameters"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Используем тот же URL, что и приложение (всегда БД lse_trading)
if [ -z "$DATABASE_URL" ] && [ -f config.env ]; then
  export DATABASE_URL=$(grep -E '^DATABASE_URL=' config.env | cut -d= -f2-)
fi
if [ -z "$DATABASE_URL" ]; then
  echo "❌ Задайте DATABASE_URL или создайте config.env с DATABASE_URL=postgresql://user:pass@host:5432/lse_trading"
  exit 1
fi
# Явно брать lse_trading (как в config_loader), иначе дамп мог идти из другой БД
DUMP_URL=$(python3 -c "
from config_loader import get_database_url
print(get_database_url())
" 2>/dev/null) || DUMP_URL="$DATABASE_URL"
# Убираем ?options= из URL для pg_dump (не мешает, но на всякий случай)
DUMP_URL="${DUMP_URL%%\?*}"

OUTPUT="${1:-lse_trading_dump_$(date +%Y%m%d_%H%M%S).sql.gz}"
echo "📤 Экспорт в $OUTPUT (только таблицы LSE: $LSE_TABLES)..."
TABLES_ARG=""
for t in $LSE_TABLES; do
  TABLES_ARG="$TABLES_ARG -t public.$t"
done
pg_dump "$DUMP_URL" --no-owner --no-acl --clean --if-exists $TABLES_ARG | gzip > "$OUTPUT"
echo "✅ Готово: $(ls -lh "$OUTPUT")"
echo "   Перенесите файл на новый сервер и выполните restore (см. docs/MIGRATE_SERVER.md)."

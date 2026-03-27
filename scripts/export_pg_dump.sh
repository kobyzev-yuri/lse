#!/bin/bash
# Экспорт дампа PostgreSQL — только таблицы LSE (без чужих схем tbl_*, vanna_vectors и т.д.).
# Запуск: на СТАРОМ сервере или с машины, где есть доступ к текущей БД.
# Требует: DATABASE_URL или config.env. pg_dump: из PATH или через Docker (контейнер lse-postgres).

set -e
set -o pipefail

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

# С хоста VM (не из контейнера) hostname postgres из docker-compose недоступен — подключаемся на localhost
if [ ! -f /.dockerenv ] && [ -z "${LSE_PG_DUMP_URL:-}" ]; then
  DUMP_URL=$(DUMP_URL="$DUMP_URL" python3 -c "
from urllib.parse import urlparse, urlunparse
import os
u = urlparse(os.environ['DUMP_URL'])
if u.hostname in ('postgres', 'lse-postgres'):
    port = u.port or 5432
    if u.username is not None:
        auth = u.username
        if u.password is not None:
            auth += ':' + u.password
        netloc = f'{auth}@127.0.0.1:{port}'
    else:
        netloc = f'127.0.0.1:{port}'
    print(urlunparse((u.scheme, netloc, u.path, '', '', '')))
else:
    print(os.environ['DUMP_URL'])
")
fi
if [ -n "${LSE_PG_DUMP_URL:-}" ]; then
  DUMP_URL="${LSE_PG_DUMP_URL%%\?*}"
fi

OUTPUT="${1:-lse_trading_dump_$(date +%Y%m%d_%H%M%S).sql.gz}"

MIN_BYTES="${LSE_PG_DUMP_MIN_BYTES:-300}"

DBNAME=$(DUMP_URL="$DUMP_URL" python3 -c "
from urllib.parse import urlparse
import os
p = urlparse(os.environ['DUMP_URL']).path.strip('/').split('?')[0]
print(p or 'lse_trading')
")

PG_CONTAINER=""
if command -v pg_dump >/dev/null 2>&1; then
  :
elif [ -n "${LSE_PG_DUMP_DOCKER:-}" ]; then
  PG_CONTAINER="$LSE_PG_DUMP_DOCKER"
elif docker ps -q -f name=lse-postgres 2>/dev/null | head -1 | grep -q .; then
  PG_CONTAINER=$(docker ps -q -f name=lse-postgres | head -1)
  echo "ℹ️  pg_dump не найден в PATH — используем docker exec $PG_CONTAINER (lse-postgres)"
else
  echo "❌ Не найден pg_dump и контейнер lse-postgres."
  echo "   Установите клиент: sudo apt install postgresql-client"
  echo "   Или укажите контейнер: export LSE_PG_DUMP_DOCKER=\$(docker ps -qf name=lse-postgres | head -1)"
  exit 1
fi

if [ "${LSE_PG_DUMP_FULL:-}" = "1" ]; then
  echo "📤 Полный дамп БД в $OUTPUT (все схемы/таблицы, без --clean)..."
  if [ -n "$PG_CONTAINER" ]; then
    docker exec -i "$PG_CONTAINER" pg_dump -U postgres -d "$DBNAME" --no-owner --no-acl | gzip > "$OUTPUT"
  else
    pg_dump "$DUMP_URL" --no-owner --no-acl | gzip > "$OUTPUT"
  fi
else
  echo "📤 Экспорт в $OUTPUT (только таблицы LSE: $LSE_TABLES)..."
  TABLES_ARG=""
  for t in $LSE_TABLES; do
    TABLES_ARG="$TABLES_ARG -t public.$t"
  done
  if [ -n "$PG_CONTAINER" ]; then
    docker exec -i "$PG_CONTAINER" pg_dump -U postgres -d "$DBNAME" --no-owner --no-acl --clean --if-exists $TABLES_ARG | gzip > "$OUTPUT"
  else
    pg_dump "$DUMP_URL" --no-owner --no-acl --clean --if-exists $TABLES_ARG | gzip > "$OUTPUT"
  fi
fi

SIZE=$(wc -c <"$OUTPUT" | tr -d ' ')
if [ "${SIZE:-0}" -lt "$MIN_BYTES" ]; then
  echo "❌ Дамп подозрительно маленький (${SIZE} байт). Частые причины:"
  echo "   • Ошибка pg_dump / пустая БД / нет доступа к PostgreSQL"
  echo "   • С хоста без Docker-клиента: sudo apt install postgresql-client или дамп через docker exec (см. скрипт)"
  echo "   • Вручную URL: export LSE_PG_DUMP_URL='postgresql://USER:PASS@127.0.0.1:5432/lse_trading'"
  rm -f "$OUTPUT"
  exit 1
fi

echo "✅ Готово: $(ls -lh "$OUTPUT")"
echo "   Перенесите файл на новый сервер и выполните restore (см. docs/MIGRATE_SERVER.md)."

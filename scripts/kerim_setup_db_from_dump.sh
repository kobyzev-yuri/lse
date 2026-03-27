#!/bin/bash
# Развёртывание базы LSE из дампа (для стороннего хоста, например Platform Керима).
# Нужны: PostgreSQL с расширением pgvector (рекомендуется образ pgvector/pgvector:pg15),
#         клиентские утилиты psql, gunzip.
#
# Примеры:
#
# 1) Postgres в Docker уже запущен (контейнер с именем my-pg, БД lse_trading создана при старте):
#    ./scripts/kerim_setup_db_from_dump.sh ./lse_trading_dump_20260327.sql.gz --docker my-pg
#
# 2) Локальный/удалённый Postgres, строка подключения:
#    export DATABASE_URL='postgresql://postgres:SECRET@127.0.0.1:5432/lse_trading'
#    ./scripts/kerim_setup_db_from_dump.sh ./lse_trading_dump.sql.gz --create-db
#
# 3) Только восстановление в уже существующую пустую БД:
#    export DATABASE_URL='postgresql://user:pass@host:5432/lse_trading'
#    ./scripts/kerim_setup_db_from_dump.sh ./dump.sql.gz
#
# Docker без compose (один контейнер с pgvector):
#    docker run -d --name kerim-pg -e POSTGRES_PASSWORD=secret -e POSTGRES_DB=lse_trading \
#      -p 5432:5432 pgvector/pgvector:pg15
#    ./scripts/kerim_setup_db_from_dump.sh ./dump.sql.gz --docker kerim-pg
#
# Имя БД в Docker по умолчанию lse_trading (как в POSTGRES_DB). Для нестандартного имени:
#    export KERIM_TARGET_DB=mydb
#    ./scripts/kerim_setup_db_from_dump.sh ./dump.sql.gz --docker mycontainer

set -e

TARGET_DB="${KERIM_TARGET_DB:-lse_trading}"

DUMP_FILE=""
DOCKER_CONTAINER=""
CREATE_DB=0

usage() {
  sed -n '1,35p' "$0" | grep -E '^#' | sed 's/^# //' | sed 's/^#//'
  echo ""
  echo "Usage: $0 <dump.sql.gz> [--docker CONTAINER] [--create-db]"
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --docker)
      DOCKER_CONTAINER="${2:?}"
      shift 2
      ;;
    --create-db)
      CREATE_DB=1
      shift
      ;;
    *)
      if [ -z "$DUMP_FILE" ]; then
        DUMP_FILE="$1"
        shift
      else
        echo "❌ Неизвестный аргумент: $1"
        usage 1
      fi
      ;;
  esac
done

if [ -z "$DUMP_FILE" ]; then
  echo "❌ Укажите путь к дампу: $0 path/to/lse_trading_dump.sql.gz"
  usage 1
fi

if [ ! -f "$DUMP_FILE" ]; then
  echo "❌ Файл не найден: $DUMP_FILE"
  exit 1
fi

if [ -n "$DOCKER_CONTAINER" ]; then
  CID=$(docker ps -q -f "name=${DOCKER_CONTAINER}" | head -1)
  if [ -z "$CID" ]; then
    echo "❌ Контейнер не найден или не запущен: $DOCKER_CONTAINER (укажите уникальное имя или ID)"
    exit 1
  fi
  echo "🐳 Docker: $DOCKER_CONTAINER → $CID · БД: $TARGET_DB"
fi

# --- подключение к целевой БД ---
if [ -n "$DOCKER_CONTAINER" ]; then
  PSQL_BASE=(docker exec -i "$CID" psql -U postgres -d "$TARGET_DB")
elif [ -n "$DATABASE_URL" ]; then
  PSQL_BASE=(psql "$DATABASE_URL")
else
  echo "❌ Задайте DATABASE_URL (postgresql://user:pass@host:port/lse_trading) или используйте --docker CONTAINER"
  exit 1
fi

admin_url_from_database_url() {
  python3 -c "
from urllib.parse import urlparse, urlunparse
import os
u = urlparse(os.environ['DATABASE_URL'])
print(urlunparse((u.scheme, u.netloc, '/postgres', '', '', '')))
"
}

target_db_name_from_database_url() {
  python3 -c "
from urllib.parse import urlparse
import os
p = urlparse(os.environ['DATABASE_URL']).path.strip('/').split('?')[0]
print(p or 'postgres')
"
}

if [ "$CREATE_DB" = "1" ] && [ -z "$DOCKER_CONTAINER" ]; then
  if [ -z "$DATABASE_URL" ]; then
    echo "❌ --create-db требует DATABASE_URL"
    exit 1
  fi
  DBNAME="$(target_db_name_from_database_url)"
  if [ "$DBNAME" = "postgres" ]; then
    echo "❌ DATABASE_URL должен указывать на целевую БД (например .../lse_trading), не .../postgres"
    exit 1
  fi
  ADMIN_URL="$(admin_url_from_database_url)"
  echo "📦 Проверка БД «$DBNAME»..."
  EXISTS="$(psql "$ADMIN_URL" -tAc "SELECT 1 FROM pg_database WHERE datname = '$DBNAME'" 2>/dev/null || true)"
  if [ -z "$EXISTS" ]; then
    echo "📦 CREATE DATABASE $DBNAME ..."
    psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DBNAME};"
  else
    echo "✅ База «$DBNAME» уже существует"
  fi
fi

echo "📌 Расширение vector (pgvector)..."
if [ -n "$DOCKER_CONTAINER" ]; then
  docker exec -i "$CID" psql -U postgres -d "$TARGET_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || {
    echo "❌ Не удалось создать расширение vector. Используйте образ с pgvector, например: pgvector/pgvector:pg15"
    exit 1
  }
else
  "${PSQL_BASE[@]}" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || {
    echo "❌ Не удалось создать расширение vector. Установите pgvector на сервере или используйте образ pgvector/pgvector."
    exit 1
  }
fi

echo "📥 Восстановление из $DUMP_FILE ..."
strip_timeout() {
  grep -v 'transaction_timeout' || true
}

dump_stream() {
  case "$DUMP_FILE" in
    *.gz) gunzip -c "$DUMP_FILE" ;;
    *)    cat "$DUMP_FILE" ;;
  esac
}

if [ -n "$DOCKER_CONTAINER" ]; then
  dump_stream | strip_timeout | docker exec -i "$CID" psql -U postgres -d "$TARGET_DB" --set ON_ERROR_STOP=on
else
  dump_stream | strip_timeout | psql "$DATABASE_URL" --set ON_ERROR_STOP=on
fi

echo "✅ Готово. Проверка:"
if [ -n "$DOCKER_CONTAINER" ]; then
  docker exec -i "$CID" psql -U postgres -d "$TARGET_DB" -c \
    "SELECT schemaname||'.'||relname AS tbl, n_live_tup AS est FROM pg_stat_user_tables ORDER BY n_live_tup DESC NULLS LAST LIMIT 15;"
else
  psql "$DATABASE_URL" -c \
    "SELECT schemaname||'.'||relname AS tbl, n_live_tup AS est FROM pg_stat_user_tables ORDER BY n_live_tup DESC NULLS LAST LIMIT 15;"
fi

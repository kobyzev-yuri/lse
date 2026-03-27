#!/bin/bash
# Дамп PostgreSQL на удалённом сервере и скачивание файла локально.
#
# На сервере вызывается scripts/export_pg_dump.sh (нужны pg_dump и config.env с DATABASE_URL,
# либо export DATABASE_URL в shell на сервере).
#
# Использование (с локальной машины):
#   export LSE_SERVER=ai8049520@your.vm.ip
#   ./scripts/fetch_pg_dump_from_server.sh
#
# Или явно (подставьте свой user@IP, не копируйте слова user и host):
#   ./scripts/fetch_pg_dump_from_server.sh ai8049520@104.197.235.201
#   ./scripts/fetch_pg_dump_from_server.sh ai8049520@104.197.235.201 /home/ai8049520/lse ./backups
#   # удалённый каталог по умолчанию ~/lse, локально в ./backups:
#   ./scripts/fetch_pg_dump_from_server.sh ai8049520@104.197.235.201 "" ./backups
#
# Полный дамп всей БД (не только таблицы LSE):
#   LSE_PG_DUMP_FULL=1 ./scripts/fetch_pg_dump_from_server.sh
#
# Переменные:
#   LSE_SERVER      — user@host (если не передан первый аргумент)
#   LSE_REMOTE_DIR  — каталог репозитория на сервере (по умолчанию ~/lse)
#   LSE_SSH_OPTS    — доп. аргументы ssh/scp (напр. -i ~/.ssh/id_ed25519_gcp)
#   LSE_PG_DUMP_MIN_BYTES — минимальный размер дампа (по умолчанию 300)

set -e
set -o pipefail

MIN_BYTES="${LSE_PG_DUMP_MIN_BYTES:-300}"
# Доп. аргументы ssh/scp из LSE_SSH_OPTS (например: -i ~/.ssh/id_ed25519)
read -r -a _SSH_EXTRA <<< "${LSE_SSH_OPTS:-}"
SSH_BASE=(ssh "${_SSH_EXTRA[@]}" -o ConnectTimeout=30 -o ServerAliveInterval=15)
SCP_BASE=(scp "${_SSH_EXTRA[@]}" -o ConnectTimeout=30 -p)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Первый аргумент — реальный SSH user@host; литерал «user@host» из документации игнорируется, если задан LSE_SERVER
if [ -n "${1:-}" ]; then
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    user@host)
      if [ -n "${LSE_SERVER:-}" ]; then
        SERVER="$LSE_SERVER"
        echo "ℹ️  Первый аргумент «$1» — плейсхолдер; используется LSE_SERVER=$SERVER"
      else
        echo "❌ Не используйте литерал user@host. Пример: $0 ai8049520@104.197.235.201"
        echo "   Или: export LSE_SERVER='ai8049520@104.197.235.201' && $0"
        exit 1
      fi
      ;;
    *)
      SERVER="$1"
      ;;
  esac
else
  SERVER="${LSE_SERVER:?Задайте сервер: первый аргумент (user@ip) или export LSE_SERVER=user@ip}"
fi

# cd на сервере: по умолчанию ~/lse удалённого пользователя (без локальной подстановки ~)
if [ -n "${2:-}" ]; then
  if [[ "$2" == *"/USER/"* ]] || [[ "$2" == "/home/USER/lse" ]]; then
    echo "ℹ️  Второй аргумент «$2» — плейсхолдер; на сервере выполняется cd ~/lse"
    REMOTE_CD='cd ~/lse'
  else
    REMOTE_CD="cd $(printf %q "$2")"
  fi
elif [ -n "${LSE_REMOTE_DIR:-}" ]; then
  REMOTE_CD="cd $(printf %q "$LSE_REMOTE_DIR")"
else
  REMOTE_CD='cd ~/lse'
fi

LOCAL_TARGET="${3:-$PROJECT_ROOT}"

REMOTE_TS="$(date +%Y%m%d_%H%M%S)"
REMOTE_FILE="/tmp/lse_trading_dump_${REMOTE_TS}.sql.gz"

echo "🖥  Сервер: $SERVER"
echo "📁 Удалённый каталог LSE: $REMOTE_CD"
echo "📤 Временный файл на сервере: $REMOTE_FILE"

# Сборка удалённой команды: опционально полный дамп
EXTRA=""
if [ "${LSE_PG_DUMP_FULL:-}" = "1" ]; then
  EXTRA="export LSE_PG_DUMP_FULL=1; "
  echo "ℹ️  Режим: полный дамп БД (LSE_PG_DUMP_FULL=1)"
else
  echo "ℹ️  Режим: только таблицы LSE (см. export_pg_dump.sh)"
fi

echo "🔧 Создание дампа на сервере..."
"${SSH_BASE[@]}" "$SERVER" "bash -lc 'set -e; set -o pipefail; ${EXTRA}${REMOTE_CD} && ./scripts/export_pg_dump.sh ${REMOTE_FILE}'"

REMOTE_SZ=$("${SSH_BASE[@]}" "$SERVER" "stat -c%s \"$REMOTE_FILE\"" 2>/dev/null || echo 0)
if [ "${REMOTE_SZ:-0}" -lt "$MIN_BYTES" ]; then
  echo "❌ На сервере дамп слишком маленький (${REMOTE_SZ} байт, минимум ${MIN_BYTES})."
  echo "   Проверьте на сервере: cd ~/lse && ./scripts/export_pg_dump.sh /tmp/test.sql.gz"
  "${SSH_BASE[@]}" "$SERVER" "rm -f ${REMOTE_FILE}" 2>/dev/null || true
  exit 1
fi
echo "📊 Размер на сервере: ${REMOTE_SZ} байт"

# Куда сохранить локально
if [ -d "$LOCAL_TARGET" ]; then
  LOCAL_FILE="${LOCAL_TARGET%/}/lse_trading_dump_${REMOTE_TS}.sql.gz"
else
  LOCAL_FILE="$LOCAL_TARGET"
fi

echo "⬇️  Скачивание → $LOCAL_FILE"
"${SCP_BASE[@]}" "${SERVER}:${REMOTE_FILE}" "$LOCAL_FILE"

"${SSH_BASE[@]}" "$SERVER" "rm -f ${REMOTE_FILE}"

LOCAL_SZ=$(wc -c <"$LOCAL_FILE" | tr -d ' ')
if [ "${LOCAL_SZ:-0}" -lt "$MIN_BYTES" ]; then
  echo "❌ После scp файл слишком маленький (${LOCAL_SZ} байт)."
  exit 1
fi

echo "✅ Готово: $(ls -lh "$LOCAL_FILE") (${LOCAL_SZ} байт)"
echo "   Восстановление локально: gunzip -c \"$LOCAL_FILE\" | psql \"\$DATABASE_URL\""
echo "   Или на сервере: ./scripts/restore_pg_dump.sh \"$LOCAL_FILE\" (после scp на сервер)"

#!/bin/bash
# Копирование config.env на сервер (VM). Запускать с локальной машины.
#
# Использование:
#   export LSE_SERVER=ai8049520@34.61.43.172   # или user@IP
#   ./scripts/sync_config_to_server.sh
#
# Или один аргумент: user@host
#   ./scripts/sync_config_to_server.sh ai8049520@34.61.43.172

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$REPO_DIR/config.env"

if [ -n "$1" ]; then
    LSE_SERVER="$1"
fi

SERVER="${LSE_SERVER:-ai8049520@104.197.235.201}"
REMOTE_PATH="~/lse/config.env"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Файл не найден: $CONFIG_FILE"
    exit 1
fi

echo "Синхронизация config.env на сервер: $SERVER"
scp "$CONFIG_FILE" "${SERVER}:${REMOTE_PATH}"
echo "✅ Готово: config.env скопирован в $SERVER:${REMOTE_PATH}"

#!/usr/bin/env bash
# Собрать crontab из ct.txt в корне репо (плейсхолдеры @LSE_HOME@, @CONTAINER@) и установить.
#
# На инстансе (пример):
#   cd ~/lse && git pull && bash scripts/install_crontab.sh /home/ai8049520 lse-bot
#
# Переменные вместо аргументов:
#   LSE_HOME=/home/ai8049520 LSE_CONTAINER=lse-bot bash scripts/install_crontab.sh
#
# ВНИМАНИЕ: команда crontab ЗАМЕНЯЕТ весь crontab текущего пользователя. Для ВМ,
# где кроме LSE ничего нет в cron — нормально. Иначе сначала crontab -l > backup.txt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${CRONTAB_TEMPLATE:-$ROOT/ct.txt}"

LSE_HOME_PATH="${1:-${LSE_HOME:-}}"
CONTAINER_NAME="${2:-${LSE_CONTAINER:-lse-bot}}"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "Нет файла crontab: $TEMPLATE (задайте CRONTAB_TEMPLATE=... при необходимости)" >&2
  exit 1
fi
if [[ -z "$LSE_HOME_PATH" ]]; then
  echo "Задайте каталог LSE на инстансе (логи: \$LSE_HOME/logs/):" >&2
  echo "  $0 /home/USER/lse [container]" >&2
  echo "или: LSE_HOME=/home/USER/lse $0" >&2
  exit 1
fi

# Без завершающего слэша в подстановке
LSE_HOME_PATH="${LSE_HOME_PATH%/}"

OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

sed \
  -e "s|@LSE_HOME@|${LSE_HOME_PATH}|g" \
  -e "s|@CONTAINER@|${CONTAINER_NAME}|g" \
  "$TEMPLATE" > "$OUT"

# Убрать комментарии и пустые строки — cron их принимает, но для наглядности можно оставить;
# crontab на Linux обычно допускает строки с # в начале.
crontab "$OUT"

echo "Crontab установлен (LSE_HOME=$LSE_HOME_PATH, CONTAINER=$CONTAINER_NAME)."
echo "Проверка: crontab -l"

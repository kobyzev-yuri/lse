#!/usr/bin/env bash
# Бэкап БД и основных конфигов в /home_cnn_rescue/lse
# Запуск:  bash scripts/backup_to_rescue.sh   или  ./scripts/backup_to_rescue.sh
# С sudo:  sudo bash /home/cnn/lse/scripts/backup_to_rescue.sh  (без chmod)

set -e
RESCUE_DIR="/home_cnn_rescue/lse"
LSE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$LSE_ROOT/config.env"

if [[ ! -f "$CONFIG" ]]; then
  echo "Не найден $CONFIG"
  exit 1
fi

# DATABASE_URL из config.env (однострочный, без экспорта в shell)
DATABASE_URL=$(grep -E '^DATABASE_URL=' "$CONFIG" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
if [[ -z "$DATABASE_URL" ]]; then
  echo "DATABASE_URL не задан в config.env"
  exit 1
fi

mkdir -p "$RESCUE_DIR"
STAMP=$(date +%Y%m%d_%H%M)
DUMP_FILE="$RESCUE_DIR/lse_trading_backup_${STAMP}.sql"

echo "Дамп БД в $DUMP_FILE ..."
pg_dump "$DATABASE_URL" -F p -f "$DUMP_FILE"
echo "Дамп готов: $(du -h "$DUMP_FILE" | cut -f1)"

echo "Копирование конфигов в $RESCUE_DIR ..."
cp -p "$LSE_ROOT/config.env" "$RESCUE_DIR/config.env"
cp -p "$LSE_ROOT/config.env.example" "$RESCUE_DIR/config.env.example"
for f in "$LSE_ROOT"/.last_signal_sent_*; do
  [[ -f "$f" ]] && cp -p "$f" "$RESCUE_DIR/" && echo "  $(basename "$f")"
done
echo "config.env, config.env.example и .last_signal_sent_* скопированы."

# Ссылка "последний дамп" для удобства
ln -sf "$(basename "$DUMP_FILE")" "$RESCUE_DIR/lse_trading_backup_latest.sql"
echo "Готово. Последний дамп: $RESCUE_DIR/lse_trading_backup_latest.sql"

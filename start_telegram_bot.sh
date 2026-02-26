#!/bin/bash
# Старт Telegram бота (polling).
# Запуск из корня проекта: ./start_telegram_bot.sh

set -e
cd "$(dirname "$0")"
PROJECT_DIR="$PWD"

# Опционально: активация venv
if [ -d ".venv" ]; then
    . .venv/bin/activate
elif [ -d "venv" ]; then
    . venv/bin/activate
fi

if [ ! -f config.env ]; then
    echo "❌ config.env не найден. Скопируйте config.env.example в config.env и укажите TELEGRAM_BOT_TOKEN."
    exit 1
fi

echo "Запуск Telegram бота (polling)..."
echo "  Каталог: $PROJECT_DIR"
echo "  Остановка: Ctrl+C"
echo ""
exec python scripts/run_telegram_bot.py

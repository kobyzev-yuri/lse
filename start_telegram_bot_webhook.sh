#!/bin/bash
# Старт Telegram бота в режиме webhook (FastAPI сервер).
# Запуск из корня проекта: ./start_telegram_bot_webhook.sh
#
# После запуска нужно:
# 1. Обеспечить HTTPS-доступ к серверу (ngrok, Cloud Run, reverse proxy).
# 2. Настроить webhook: python scripts/setup_webhook.py --url https://YOUR_PUBLIC_URL/webhook

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

PORT="${PORT:-8080}"
echo "Запуск Telegram бота (webhook) на порту $PORT..."
echo "  Каталог: $PROJECT_DIR"
echo "  Endpoint: http://0.0.0.0:$PORT/webhook"
echo "  Остановка: Ctrl+C"
echo ""
exec python api/bot_app.py

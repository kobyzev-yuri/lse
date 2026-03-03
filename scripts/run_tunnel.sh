#!/bin/bash
# Запуск туннеля к локальному порту 8080 (для webhook бота).
# Даёт публичный HTTPS URL вида https://XXX.trycloudflare.com
#
# Использование:
#   1. В одном терминале: ./start_telegram_bot_webhook.sh
#   2. В другом:           ./scripts/run_tunnel.sh
#   3. Скопировать из вывода URL и: python scripts/setup_webhook.py --url https://XXX.trycloudflare.com/webhook

set -e
cd "$(dirname "$0")/.."
PROJECT_DIR="$PWD"

CLOUDFLARED=""
for candidate in "$PROJECT_DIR/scripts/bin/cloudflared" "$(command -v cloudflared)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    CLOUDFLARED="$candidate"
    break
  fi
done

if [ -z "$CLOUDFLARED" ]; then
  echo "cloudflared не найден, пробуем скачать в scripts/bin/..."
  mkdir -p "$PROJECT_DIR/scripts/bin"
  if curl -sL --connect-timeout 30 --max-time 120 \
     "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
     -o "$PROJECT_DIR/scripts/bin/cloudflared" && [ -s "$PROJECT_DIR/scripts/bin/cloudflared" ]; then
    chmod +x "$PROJECT_DIR/scripts/bin/cloudflared"
    CLOUDFLARED="$PROJECT_DIR/scripts/bin/cloudflared"
  else
    echo "❌ Не удалось скачать. Установите вручную:"
    echo "   curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o scripts/bin/cloudflared"
    echo "   chmod +x scripts/bin/cloudflared"
    exit 1
  fi
fi

PORT="${PORT:-8080}"
echo "Туннель к http://localhost:$PORT (остановка: Ctrl+C)"
echo ""

# Запускаем туннель; URL появится в выводе через несколько секунд
"$CLOUDFLARED" tunnel --url "http://localhost:$PORT" 2>&1 | while IFS= read -r line; do
  echo "$line"
  if echo "$line" | grep -q "trycloudflare.com"; then
    url=$(echo "$line" | grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | head -1)
    if [ -n "$url" ]; then
      echo ""
      echo ">>> Публичный URL: $url"
      echo ">>> Webhook для Telegram: ${url}/webhook"
      echo ">>> Настройка: python scripts/setup_webhook.py --url ${url}/webhook"
      echo ""
    fi
  fi
done

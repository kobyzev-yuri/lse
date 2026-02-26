#!/bin/bash
# Старт веб-интерфейса (FastAPI + uvicorn).
# Запуск из корня проекта: ./start_web.sh [порт]
# По умолчанию порт 8000. Открыть: http://localhost:8000

set -e
cd "$(dirname "$0")"
PROJECT_DIR="$PWD"

# Опционально: активация venv
if [ -d ".venv" ]; then
    . .venv/bin/activate
elif [ -d "venv" ]; then
    . venv/bin/activate
fi

PORT="${1:-8000}"
echo "Запуск веб-интерфейса на порту $PORT..."
echo "  Каталог: $PROJECT_DIR"
echo "  URL:     http://localhost:$PORT"
echo "  Остановка: Ctrl+C"
echo ""
exec python -m uvicorn web_app:app --host 0.0.0.0 --port "$PORT"

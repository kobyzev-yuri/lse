# LSE Telegram Bot (webhook) для Google Cloud Run
# По аналогии с проектом sc: сборка из корня репозитория, порт 8080
FROM python:3.11-slim

WORKDIR /app

# Системные зависимости (для сборки пакетов и matplotlib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
EXPOSE 8080

# Webhook-сервер: FastAPI + uvicorn. Конфиг — только из переменных окружения (config.env не копируем).
CMD ["uvicorn", "api.bot_app:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "60"]

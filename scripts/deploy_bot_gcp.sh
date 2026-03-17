#!/bin/bash
# Деплой LSE Telegram Bot (webhook) в Google Cloud Run
# По аналогии с sc/api/deploy_api.sh. Запускать из корня репозитория: ./scripts/deploy_bot_gcp.sh

set -e

PROJECT_ID=${PROJECT_ID:-}
REGION=${REGION:-us-central1}
SERVICE_NAME=${SERVICE_NAME:-lse-telegram-bot}
IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "🚀 Деплой LSE Telegram Bot в Google Cloud Run"
echo "=============================================="
echo ""

if [ -z "$PROJECT_ID" ]; then
    echo "❌ Задайте PROJECT_ID (например: export PROJECT_ID=my-gcp-project)"
    exit 1
fi

echo "📋 Параметры:"
echo "   Проект: $PROJECT_ID"
echo "   Регион: $REGION"
echo "   Сервис: $SERVICE_NAME"
echo "   Образ:  $IMAGE_NAME"
echo ""

if ! command -v gcloud &> /dev/null; then
    echo "❌ gcloud CLI не установлен"
    exit 1
fi

gcloud config set project "$PROJECT_ID"

echo "🔨 Сборка Docker-образа (контекст: корень репозитория)..."
gcloud builds submit --tag "$IMAGE_NAME" --project="$PROJECT_ID" .

echo "🚀 Развёртывание в Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE_NAME" \
    --platform managed \
    --region "$REGION" \
    --allow-unauthenticated \
    --memory 1Gi \
    --cpu 1 \
    --max-instances 5 \
    --min-instances 0 \
    --port 8080 \
    --timeout 60 \
    --project="$PROJECT_ID"

echo ""
echo "✅ Деплой завершён."
echo ""
echo "📝 URL сервиса (установите webhook после настройки секретов):"
gcloud run services describe "$SERVICE_NAME" --region="$REGION" --format="value(status.url)" --project="$PROJECT_ID"
echo ""
echo "Далее: задайте переменные окружения (TELEGRAM_BOT_TOKEN, DATABASE_URL и др.) в Cloud Run и выполните:"
echo "  python scripts/setup_webhook.py --url <URL_СЕРВИСА>/webhook"
echo "См. docs/DEPLOY_GCP.md"

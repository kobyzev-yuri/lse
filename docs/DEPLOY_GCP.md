# Деплой LSE Telegram Bot в Google Cloud Run

Бот работает в режиме **webhook**: Telegram шлёт обновления на ваш URL. Так можно обойти блокировки Telegram в РФ, развернув сервис в GCP (США/Европа).

По аналогии с проектом **sc**: сборка Docker-образа через Cloud Build, развёртывание в Cloud Run.

---

## 1. Предварительные условия

- Аккаунт Google Cloud, проект (или создайте новый).
- Установленный [gcloud CLI](https://cloud.google.com/sdk/docs/install).
- Код в `main` запушен на GitHub (для воспроизводимости; сборка идёт из локального контекста).

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

Включите нужные API:

```bash
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
```

---

## 2. Переменные окружения (секреты)

В Cloud Run задайте переменные окружения. Минимум для бота:

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `DATABASE_URL` | PostgreSQL (например `postgresql://user:pass@host:5432/lse_trading`). БД должна быть доступна из интернета (Cloud SQL с публичным IP или внешний хостинг). |
| `TELEGRAM_ALLOWED_USERS` | Список user_id через запятую (опционально) |

Остальные параметры из `config.env` (LLM, новости, и т.д.) задайте при необходимости. Конфиг читается из **переменных окружения** (файл `config.env` в образ не копируется).

**Как задать в Cloud Run:**

- Через консоль: Cloud Run → сервис → Edit & Deploy → Variables & Secrets.
- Или при деплое:
  ```bash
  gcloud run deploy lse-telegram-bot ... \
    --set-env-vars "TELEGRAM_BOT_TOKEN=xxx,DATABASE_URL=postgresql://..."
  ```
- Секреты через Secret Manager (рекомендуется для токена):
  ```bash
  echo -n "YOUR_BOT_TOKEN" | gcloud secrets create telegram-bot-token --data-file=-
  gcloud run deploy lse-telegram-bot ... \
    --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bot-token:latest"
  ```

---

## 3. Деплой

Из **корня репозитория** lse:

```bash
export PROJECT_ID=your-gcp-project-id
./scripts/deploy_bot_gcp.sh
```

Или вручную:

```bash
PROJECT_ID=your-gcp-project-id
REGION=us-central1
SERVICE_NAME=lse-telegram-bot
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

gcloud config set project ${PROJECT_ID}
gcloud builds submit --tag ${IMAGE} --project=${PROJECT_ID} .
gcloud run deploy ${SERVICE_NAME} \
  --image ${IMAGE} \
  --platform managed \
  --region ${REGION} \
  --no-allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --port 8080 \
  --timeout 60 \
  --set-env-vars "TELEGRAM_BOT_TOKEN=...,DATABASE_URL=..." \
  --project=${PROJECT_ID}
```

Сервис по умолчанию **не публичный** (`--no-allow-unauthenticated`). Доступ по URL возможен только с учётом IAM или если позже включите доступ для незарегистрированных (тогда webhook сможет слать запросы без авторизации GCP).

**Важно:** для webhook Telegram должен достучаться до URL. Если оставить `--no-allow-unauthenticated`, только авторизованные в GCP смогут вызывать URL; Telegram к нему не подойдёт. Нужно либо:

- сделать сервис **доступным для всех** (только webhook endpoint, без чувствительных данных в ответах), либо  
- использовать Load Balancer + IAP или другой способ, оставив endpoint публичным только для Telegram IP (сложнее).

На практике для webhook обычно делают **allow-unauthenticated** (как в sc для API), а защиту обеспечивают токеном бота и проверкой в коде. Пример:

```bash
gcloud run deploy ${SERVICE_NAME} \
  ... \
  --allow-unauthenticated
```

Тогда URL вида `https://lse-telegram-bot-xxxxx.run.app` будет доступен Telegram для POST `/webhook`.

---

## 4. Настройка webhook в Telegram

После деплоя возьмите URL сервиса:

```bash
gcloud run services describe lse-telegram-bot --region=us-central1 --format="value(status.url)" --project=YOUR_PROJECT_ID
```

Установите webhook (локально, с настроенным `config.env` или только с `TELEGRAM_BOT_TOKEN` в env):

```bash
python scripts/setup_webhook.py --url https://YOUR_SERVICE_URL/webhook
```

Проверка:

```bash
curl https://YOUR_SERVICE_URL/webhook/info
```

Удаление webhook (вернуться к polling):

```bash
python scripts/setup_webhook.py --delete
```

---

## 5. База данных

PostgreSQL должна быть доступна с Cloud Run (из интернета или VPC connector).

- **Cloud SQL**: включите публичный IP или подключите Cloud Run к VPC и укажите внутренний адрес в `DATABASE_URL`.
- Внешний хостинг: укажите в `DATABASE_URL` хост, порт, пользователь, пароль, база `lse_trading`.

В `config_loader` при отсутствии `config.env` используется только `DATABASE_URL` из переменных окружения.

---

## 6. Обновление после изменений в коде

```bash
export PROJECT_ID=your-gcp-project-id
./scripts/deploy_bot_gcp.sh
```

Webhook URL не меняется — повторно вызывать `setup_webhook.py` не нужно.

---

## Кратко по аналогии со sc

| sc (pathology-api) | lse (telegram bot) |
|--------------------|---------------------|
| `api/deploy_api.sh` | `scripts/deploy_bot_gcp.sh` |
| `uvicorn api.main:app` | `uvicorn api.bot_app:app` |
| Порт 8080 | Порт 8080 |
| Cloud Run, gcloud builds submit | То же |

Отличие: у бота нет локального `config.env` в образе, все параметры задаются переменными окружения в Cloud Run.

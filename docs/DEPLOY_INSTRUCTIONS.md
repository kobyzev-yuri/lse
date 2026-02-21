# Развёртывание LSE на Google Cloud

Два варианта: **одна VM** (всё на одном сервере) или **Cloud Run + VM** (бот на Cloud Run, БД и cron на VM).

---

## Вариант A: Одна VM (всё в одном)

Подходит для малой нагрузки: PostgreSQL (с pgvector), cron (цены, новости, торговый цикл, RSI), Telegram-бот (polling или webhook).

**Оценка стоимости:** ~$20–40/мес (e2-small / e2-medium + диск 30–50 GB в регионе europe-west1 или us-central1).

### 1. Создание VM

```bash
export PROJECT_ID=your-gcp-project
export ZONE=europe-west1-b
export VM_NAME=lse-server

gcloud compute instances create $VM_NAME \
  --project=$PROJECT_ID --zone=$ZONE \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-balanced
```

При webhook — открыть порт 8080 (firewall rule на tcp:8080).

### 2. На VM: установка

- **PostgreSQL + pgvector:** установить Postgres, создать БД `lse_trading`, пользователя, `CREATE EXTENSION vector;`
- **Python 3.11:** conda или venv
- **Код:** `git clone` репозитория в `/opt/lse`, `pip install -r requirements.txt`
- **Конфиг:** `config.env` с `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, при необходимости ключи API (NewsAPI, Alpha Vantage)
- **Инициализация:** `python init_db.py`, `./setup_cron.sh`
- **Бот:** polling — `python scripts/run_telegram_bot.py` (systemd/screen) или webhook — запуск `api/bot_app.py` на 8080 и настройка webhook на `https://VM_IP:8080/webhook` (нужен HTTPS: reverse proxy или load balancer)

Подробный порядок установки Postgres/pgvector и cron — в [CRON_TICKERS_EXPLANATION.md](CRON_TICKERS_EXPLANATION.md) и [setup_cron.sh](../setup_cron.sh).

---

## Вариант B: Cloud Run (бот) + VM (БД и cron)

Бот и API — в Cloud Run (scale-to-zero, управляемый HTTPS). БД и cron — на одной VM.

**Оценка:** VM ~$20–35/мес + Cloud Run по запросам (при малом трафике несколько $/мес).

### 1. VM для БД и cron

Как в варианте A: создать VM, установить PostgreSQL с pgvector, Python, клонировать репозиторий, `config.env`, `init_db.py`, `setup_cron.sh`. Бот на этой VM не запускать. Обеспечить доступ к БД с Cloud Run (статический IP + firewall или VPC connector).

### 2. Деплой на Cloud Run

На машине с установленным `gcloud`:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

export PROJECT_ID=your-gcp-project
export REGION=us-central1
export SERVICE_NAME=lse-bot

# Сборка (в корне репозитория, при наличии Dockerfile)
gcloud builds submit --tag gcr.io/$PROJECT_ID/$SERVICE_NAME --project=$PROJECT_ID

# Деплой
gcloud run deploy $SERVICE_NAME \
  --image gcr.io/$PROJECT_ID/$SERVICE_NAME \
  --platform managed --region $REGION --allow-unauthenticated \
  --memory 1Gi --cpu 1 --max-instances 10 --min-instances 0 \
  --port 8080 --timeout 60 \
  --set-env-vars "DATABASE_URL=postgresql://user:pass@YOUR_DB_HOST:5432/lse_trading" \
  --set-env-vars "TELEGRAM_BOT_TOKEN=..." \
  --project=$PROJECT_ID
```

URL сервиса: `gcloud run services describe $SERVICE_NAME --region=$REGION --format="value(status.url)"`. Webhook: `https://api.telegram.org/bot<TOKEN>/setWebhook?url=<SERVICE_URL>/webhook`.

Секреты предпочтительно задавать через Secret Manager и подключать к Cloud Run через `--set-secrets`.

### 3. Переменные окружения (ориентир)

| Переменная | Описание |
|------------|----------|
| `DATABASE_URL` | Строка подключения к PostgreSQL (хост VM или Cloud SQL). |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather. |
| При необходимости | Ключи NewsAPI, Alpha Vantage и т.д. |

---

## Проверка

- **Cloud Run:** `gcloud run services logs read $SERVICE_NAME --region=$REGION --limit=50`
- **Бот:** отправить сообщение в Telegram, проверить логи; команды `/status`, `/news` должны работать без ошибок БД.

---

## Сравнение вариантов

| Вариант | Оценка стоимости | Заметки |
|---------|-------------------|--------|
| Одна VM (e2-small) | ~$20–25/мес | Postgres + cron + бот (polling). Проще всего. |
| Одна VM (e2-medium) | ~$30–40/мес | Запас по памяти под embedding/LLM. |
| Cloud Run + VM | ~$25–45/мес | Бот на Run, БД и cron на VM; нужен доступ Run → БД. |

Схемы потоков и размещение компонентов — в [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md) (разделы 10–11).

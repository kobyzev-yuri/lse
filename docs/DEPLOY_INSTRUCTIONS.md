# Инструкция по развёртыванию LSE (Cloud Run + отдельный сервер БД/КБ)

Схема развёртывания **аналогична проекту sc**: сервисы приложения (Telegram-бот и API) разворачиваются в **Google Cloud Run**, база данных **PostgreSQL** и базы знаний (**knowledge_base**, **trade_kb**) — на **отдельном сервере**. Когда сервер будет заведён, в переменных окружения Cloud Run указывается строка подключения к этому серверу.

## Архитектура

| Компонент | Где разворачивается |
|-----------|---------------------|
| Telegram Bot (webhook + handlers) | Google Cloud Run |
| LSE API (отчёты, статус, данные) | Google Cloud Run |
| PostgreSQL (lse_trading) | Отдельный сервер |
| Базы знаний (таблицы в той же БД) | Тот же сервер |

Подробные диаграммы — в [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md) (разделы 10 и 11).

---

## Предусловия

1. Код в ветке `main` запушен в GitHub.
2. На машине, с которой деплоите, установлен **gcloud** CLI и выполнен вход:
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```
3. **Сервер с PostgreSQL** (когда будет готов):
   - Установлен PostgreSQL с расширением **pgvector**.
   - Выполнены миграции LSE, при необходимости загружены начальные данные.
   - Доступ с Cloud Run обеспечен (например, через VPC connector, Cloud SQL Proxy или белый список IP).

---

## Деплой на Google Cloud Run

Выполнять на **локальной машине** (или CI), где настроен `gcloud`.

### Вариант 1: Автоматический деплой через скрипт

Когда в репозитории появятся `Dockerfile` и скрипт деплоя (например, `api/deploy_lse.sh` или `deploy_bot.sh`):

```bash
cd /path/to/lse
# Указать при необходимости:
# export PROJECT_ID=your-gcp-project
# export REGION=europe-west1
# export SERVICE_NAME=lse-bot
./api/deploy_lse.sh   # или путь к скрипту по факту
```

В скрипте должны быть: сборка образа через `gcloud builds submit` и деплой через `gcloud run deploy` с портом 8080 и переменными окружения (`DATABASE_URL`, `TELEGRAM_BOT_TOKEN` и т.д.).

### Вариант 2: Ручной деплой

```bash
cd /path/to/lse

export PROJECT_ID=your-gcp-project
export REGION=us-central1
export SERVICE_NAME=lse-bot

# Сборка образа (из директории с Dockerfile)
gcloud builds submit --tag gcr.io/$PROJECT_ID/$SERVICE_NAME --project=$PROJECT_ID

# Развёртывание в Cloud Run
gcloud run deploy $SERVICE_NAME \
  --image gcr.io/$PROJECT_ID/$SERVICE_NAME \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --max-instances 10 \
  --min-instances 0 \
  --port 8080 \
  --timeout 60 \
  --set-env-vars "DATABASE_URL=postgresql://user:pass@YOUR_DB_HOST:5432/lse_trading" \
  --set-env-vars "TELEGRAM_BOT_TOKEN=your_bot_token" \
  --project=$PROJECT_ID
```

После деплоя:

- Узнать URL сервиса:
  ```bash
  gcloud run services describe $SERVICE_NAME --region=$REGION --format="value(status.url)" --project=$PROJECT_ID
  ```
- Установить webhook для бота (HTTPS):
  ```bash
  curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<SERVICE_URL>/webhook"
  ```

---

## Сервер БД (отдельный сервер)

Развёртывание **на другом сервере** (не в том же GCP-проекте, что и sc) — по той же логике, что и в sc:

1. Установить PostgreSQL, включить расширение **pgvector**.
2. Создать БД `lse_trading`, пользователя с правами на неё.
3. Применить миграции LSE (схема таблиц: `quotes`, `knowledge_base`, `trade_kb`, `portfolio_state`, `trade_history` и т.д.).
4. При необходимости выполнить `init_db.py` и скрипты загрузки новостей/котировок.
5. Обеспечить сетевой доступ с Cloud Run к этому серверу (VPC / Cloud SQL Proxy / firewall с IP Cloud Run).
6. В переменной `DATABASE_URL` на Cloud Run указать хост этого сервера.

Когда сервер будет заведён, можно дополнить этот раздел конкретными командами (например, установка через Docker или пакеты ОС).

---

## Проверка после деплоя

1. **Логи Cloud Run:**
   ```bash
   gcloud run services logs read $SERVICE_NAME --region=$REGION --project=$PROJECT_ID --limit=50
   ```
2. **Проверка webhook:** отправить боту сообщение в Telegram и убедиться, что в логах есть обработка запроса.
3. **Проверка БД:** команды бота, требующие БД (например, `/status`, `/news`), должны возвращать данные без ошибок подключения.

---

## Переменные окружения (ориентир)

| Переменная | Описание |
|------------|----------|
| `DATABASE_URL` | Строка подключения к PostgreSQL (хост сервера БД). |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather. |
| При необходимости | Ключи для новостей/API (NewsAPI, Alpha Vantage и т.д.) — если бот/API их использует. |

Секреты лучше задавать через Secret Manager и подключать к Cloud Run через `--set-secrets`.

---

## Связь с другими документами

- **BUSINESS_PROCESSES.md** — разделы 10 (Telegram бот, webhook) и 11 (схема развёртывания).
- **../sc/DEPLOY_INSTRUCTIONS.md** — образец пошагового деплоя API в Cloud Run в проекте sc.

Деплой на отдельный сервер (Postgres + КБ) будет выполнен, когда сервер будет готов; инструкцию при необходимости дополним.

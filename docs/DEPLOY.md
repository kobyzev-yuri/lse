# Деплой LSE на удалённый сервер

Единая инструкция: регулярное обновление (часто) и развёртывание с нуля (редко). Детали переноса VM — в [MIGRATE_SERVER.md](MIGRATE_SERVER.md).

---

## Регулярный деплой (обновление кода и/или конфига)

Цель: максимально быстро и с минимальным участием — «сделай деплой», затем при необходимости синхронизировать GitHub и перезапустить сервисы на сервере.

### 1. У вас изменился только код (без конфига)

**На своей машине (где правите код):**

```bash
cd /path/to/lse
git add -A
git commit -m "краткое описание"
git push origin main
```

**На сервере** — один из вариантов:

- **Автоматически:** если настроен cron (см. ниже), через до 10 минут подтянется код, пересоберётся образ и перезапустится `lse`. Ничего делать не нужно.
- **Вручную сразу:**
  ```bash
  ssh ai8049520@104.197.235.201
  cd ~/lse
  ./scripts/deploy_from_github.sh
  ```
  Скрипт сам делает `git fetch` и `git pull`; отдельно pull перед деплоем не нужен. Если появились новые коммиты — пересоберёт образ и перезапустит `lse`.  
  **Принудительная пересборка** (например, после ручного `git pull` или чтобы пересобрать без новых коммитов):
  ```bash
  ./scripts/deploy_from_github.sh --force
  ```

Итог: код на сервере = последний `main`, контейнер `lse-bot` пересобран и перезапущен.

---

### 2. У вас изменился только конфиг (`config.env`)

**На своей машине:**

```bash
cd /path/to/lse
./scripts/sync_config_to_server.sh
# по умолчанию копирует в ai8049520@104.197.235.201
# иначе: ./scripts/sync_config_to_server.sh user@host
```

**На сервере** — перезапустить контейнер (конфиг монтируется с хоста в `/app/config.env`, поэтому пересоздавать контейнер не нужно):

```bash
cd ~/lse
docker compose restart lse
```

Код не трогаем, образ не пересобираем.

---

### 3. Изменились и код, и конфиг

**На своей машине:**

```bash
cd /path/to/lse
git add -A
git commit -m "описание"
git push origin main
./scripts/sync_config_to_server.sh
```

**На сервере:**

```bash
cd ~/lse
./scripts/deploy_from_github.sh --force
docker compose restart lse
```

Или одной строкой после sync: `./scripts/deploy_from_github.sh --force && docker compose restart lse`.

---

### 4. Синхронизация GitHub «в обе стороны»

- **Обычный поток:** вы правите локально → `git push origin main` → на сервере `git pull` (или скрипт деплоя делает pull). Сервер только тянет, не пушит.
- **Если правили что-то на сервере** и хотите вернуть это в репозиторий:
  - на сервере: `git add ... && git commit && git push origin main` (если на сервере есть права на push),  
  - или скопировать изменения к себе, закоммитить и запушить с локальной машины.
- **Перед деплоем** убедитесь, что всё нужное запушено в `main`, тогда на сервере `deploy_from_github.sh` подтянет актуальное.
- **Нужен ли `git pull` перед деплоем?** Нет. `./scripts/deploy_from_github.sh` сам делает `fetch` и `pull`. Достаточно выполнить `./scripts/deploy_from_github.sh` (при изменениях пересоберёт) или `./scripts/deploy_from_github.sh --force` (всегда пересборка).

---

### 5. Автоматический деплой по расписанию (cron)

На сервере скрипт раз в 10 минут проверяет `git pull`; при появлении новых коммитов пересобирает образ и перезапускает `lse`:

```bash
crontab -e
```

Добавить строку (путь подставьте свой):

```
*/10 * * * * LSE_REPO_DIR=/home/ai8049520/lse /home/ai8049520/lse/scripts/deploy_from_github.sh >> /home/ai8049520/lse/logs/deploy.log 2>&1
```

Логи: `~/lse/logs/deploy.log`. Ручной запуск: `./scripts/deploy_from_github.sh` (деплой только при изменениях) или `./scripts/deploy_from_github.sh --force` (всегда пересборка и перезапуск).

---

### 5.1. Проверка ключа OpenAI (gpt-4o) на сервере

Локально часто 403 (регион не поддерживается). На сервере (GCP и т.д.) тот же ключ обычно работает. На хосте нет пакета `openai`, поэтому проверку нужно запускать **внутри контейнера**:

```bash
ssh ai8049520@104.197.235.201
cd ~/lse
git pull   # если скрипт ещё не подтянут
docker compose exec lse python scripts/check_openai_gpt_key.py
```

Скрипт читает `OPENAI_GPT_KEY` или `OPENAI_API_KEY` из смонтированного в контейнер `config.env`. При успехе выведет «✅ Ключ работает с OpenAI gpt-4o напрямую» и подсказку по переключению на прямой API.

---

### 6. Изменение cron-задач (расписание скриптов)

Cron ставится один раз скриптом из репо (цены, торговый цикл, сигналы, новости и т.д.):

```bash
cd ~/lse
./setup_cron_docker.sh
```

Чтобы изменить расписание или добавить/убрать задачи:

```bash
crontab -e
```

Имя контейнера по умолчанию — `lse-bot`. Чтобы задать другое перед установкой крона: `export LSE_CONTAINER_NAME=lse-bot && ./setup_cron_docker.sh`.

---

### 7. Диагностика: страница «Сервис / логи»

В веб-интерфейсе по адресу **/service** (или из меню «Сервис / логи») доступна сводка для поиска проблем:

- **База данных** — подключение, последняя дата котировок, число сделок за 24 ч и новостей за 7 дней.
- **Что проверить** — подсказки при отсутствии новостей/котировок или ошибке БД.
- **Логи cron** — хвосты логов (cron_sndk_signal.log, trading_cycle, fetch_news и т.д.) с подсветкой ERROR/WARNING.
- **Watchdog** — вывод cron_watchdog.log (найденные в логах ошибки).

Чтобы логи были видны в контейнере, в `docker-compose.yml` смонтирован каталог `./logs:/app/logs:ro`. На сервере cron пишет в `~/lse/logs/`; после перезапуска контейнера (`docker compose up -d lse`) страница /service подхватит эти файлы.

**Если в `news_fetch.log` много ошибок 429 (Too Many Requests):** NewsAPI и Investing.com ограничивают число запросов. В коде включены повторные попытки с паузой (60–120 с) и паузы между источниками в cron. Если 429 не проходят — уменьшите частоту запуска новостного крона (например, раз в 2 часа вместо каждого часа) или проверьте лимиты вашего плана NewsAPI.

---

### 8. Краткий чеклист регулярного деплоя

| Что изменилось | Локально | На сервере |
|----------------|-----------|------------|
| Только код | `git push origin main` | `./scripts/deploy_from_github.sh` или дождаться cron |
| Только конфиг | `./scripts/sync_config_to_server.sh` | `docker compose restart lse` |
| Код + конфиг | `git push` + `sync_config_to_server.sh` | `./scripts/deploy_from_github.sh --force` и `docker compose restart lse` |

---

## Деплой с нуля (новый сервер, редко)

Когда меняете сервер или поднимаете LSE впервые на чистой VM.

### Шаг 1: Экспорт дампа БД (со старого сервера или с машины с доступом к БД)

На машине, где есть текущая БД LSE:

```bash
cd /path/to/lse
export DATABASE_URL="postgresql://USER:PASS@HOST:5432/lse_trading"   # или из config.env
./scripts/export_pg_dump.sh
# Создастся lse_trading_dump_YYYYMMDD_HHMMSS.sql.gz
```

Скопировать дамп на новый сервер:

```bash
scp lse_trading_dump_*.sql.gz ai8049520@104.197.235.201:~/
```

### Шаг 2: Подготовка нового сервера (GCP VM)

- Подключиться по SSH: `ssh ai8049520@104.197.235.201`
- Установить Docker и Docker Compose (см. [MIGRATE_SERVER.md](MIGRATE_SERVER.md), раздел 2.1)
- Клонировать репозиторий и настроить конфиг:

```bash
cd ~
git clone https://github.com/kobyzev-yuri/lse.git
cd lse
cp config.env.example config.env
nano config.env   # TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, и т.д.
```

При необходимости скопировать готовый `config.env` с локальной машины: `./scripts/sync_config_to_server.sh ai8049520@104.197.235.201`.

### Шаг 3: PostgreSQL и восстановление дампа

```bash
cd ~/lse
docker compose up -d postgres
sleep 10
chmod +x scripts/restore_pg_dump.sh
./scripts/restore_pg_dump.sh ~/lse_trading_dump_YYYYMMDD_HHMMSS.sql.gz
```

### Шаг 4: Запуск бота и веба

```bash
docker compose up -d
docker compose logs -f lse   # проверка
```

### Шаг 5: Cron и автодеплой

```bash
chmod +x setup_cron_docker.sh scripts/deploy_from_github.sh
./setup_cron_docker.sh
crontab -e   # при желании добавить строку для deploy_from_github.sh (см. раздел 5 выше)
```

### Шаг 6: Доступ к веб-карточкам снаружи

В GCP: **VPC → Firewall** — правило входящего **tcp:8080** (или создать вручную в консоли). Тогда URL: `http://104.197.235.201:8080/game5m/cards`.

Полный чеклист и детали (второй диск, пароль Postgres, ошибка «no space left») — в [MIGRATE_SERVER.md](MIGRATE_SERVER.md).

---

## Если deploy не проходит

**Ошибка `The following untracked working tree files would be overwritten by merge`**

На сервере есть неотслеживаемый файл с тем же именем, что и в репозитории; Git не перезаписывает его при pull. Удалите этот файл и повторите деплой (после pull придёт версия из GitHub):

```bash
cd ~/lse
rm -f scripts/check_openai_gpt_key.py   # или другой путь из сообщения об ошибке
./scripts/deploy_from_github.sh
```

Файл `check_openai_gpt_key.py` уже есть в репозитории — после pull он появится из GitHub.

---

## Полезные команды на сервере

| Задача | Команда |
|--------|---------|
| Логи бота/веба | `docker compose logs -f lse` |
| Перезапуск только lse | `docker compose restart lse` |
| Пересборка и перезапуск | `./scripts/deploy_from_github.sh --force` |
| Очистка диска и пересборка при «no space» | `./scripts/server_build_clean.sh` |
| Проверка веба локально | `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/game5m/cards` |
| Текущий cron | `crontab -l` |
| Логи автодеплоя | `tail -f ~/lse/logs/deploy.log` |

---

## Конфиг в контейнере

В `docker-compose.yml` файл `config.env` с хоста смонтирован в контейнер как `/app/config.env` (read-only). Приложение читает его при каждом обращении к настройкам.

**Порядок действий:**

1. **Один раз** после обновления репо (когда в compose появилось монтирование конфига) — применить новый compose, чтобы контейнер пересоздался с volume:
   ```bash
   cd ~/lse
   git pull
   docker compose up -d lse
   ```
2. **Дальше** при изменении только конфига: правите `~/lse/config.env` на сервере (или копируете его через `sync_config_to_server.sh`) и делаете только **restart** — пересоздавать контейнер и пересобирать образ не нужно:
   ```bash
   docker compose restart lse
   ```

На сервере перед первым запуском должен существовать файл `~/lse/config.env` (скопировать из `config.env.example` или применить `sync_config_to_server.sh`).

---

## Текущий сервер

- **Хост:** `ai8049520@104.197.235.201`
- **Конфиг на сервер:** `./scripts/sync_config_to_server.sh` (по умолчанию этот хост)
- **Веб-карточки 5m:** http://104.197.235.201:8080/game5m/cards

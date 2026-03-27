# Перенос LSE на новый сервер (VM + Docker)

Пошаговый перенос: дамп PostgreSQL, развёртывание в Docker (postgres + бот), cron через `docker exec` — по аналогии с проектом **sc** (контейнер + сервисы).

---

## 1. Экспорт дампа на старом сервере

Скрипт экспортирует **только таблицы LSE**: `quotes`, `knowledge_base`, `portfolio_state`, `trade_history`, `strategy_parameters`. Чужие схемы (tbl_*, vanna_vectors и т.д.) в дамп не попадают. Описание колонок: [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md).

На машине, где сейчас крутится LSE и есть доступ к текущей БД:

```bash
cd /path/to/lse
export DATABASE_URL="postgresql://USER:PASS@HOST:5432/lse_trading"   # или из config.env
./scripts/export_pg_dump.sh
# Создастся файл lse_trading_dump_YYYYMMDD_HHMMSS.sql.gz
```

Или явно указать имя файла:

```bash
./scripts/export_pg_dump.sh my_backup.sql.gz
```

Перенесите полученный `.sql.gz` на новый сервер (scp, rsync):

```bash
scp lse_trading_dump_*.sql.gz ai8049520@34.61.43.172:~/
```

При необходимости сделайте скрипты исполняемыми: `chmod +x scripts/export_pg_dump.sh scripts/restore_pg_dump.sh`

---

## 2. Подготовка нового сервера (GCP VM)

Зайдите по SSH на инстанс (например `ai8049520@34.61.43.172`).

### 2.1 Установка Docker и Docker Compose

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
# Выйти и зайти снова, чтобы группа docker применилась
```

### 2.2 Клонирование репозитория и конфиг

```bash
cd ~
git clone https://github.com/kobyzev-yuri/lse.git
cd lse
cp config.env.example config.env
nano config.env   # подставьте TELEGRAM_BOT_TOKEN, DATABASE_URL не трогайте для Docker — подставится в compose
```

**Важно:** в `config.env` на сервере задайте как минимум:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USERS` (опционально)
- `TELEGRAM_SIGNAL_CHAT_IDS` или `TELEGRAM_SIGNAL_CHAT_ID`
- при необходимости: `OPENAI_API_KEY`, `TICKERS_FAST`, `GAME_5M_*` и т.д.

`DATABASE_URL` для контейнера бота задаётся в `docker-compose.yml` (хост `postgres`). Если будете запускать что-то с хоста (например скрипты вручную), можно задать `DATABASE_URL=postgresql://postgres:lse_pg_pass@localhost:5432/lse_trading`.

### 2.3 Пароль Postgres в compose

По умолчанию в compose используется пароль `lse_pg_pass`. Чтобы задать свой:

```bash
export POSTGRES_PASSWORD="ваш_надёжный_пароль"
# и при запуске compose он подхватится; в config.env для хоста тогда: postgresql://postgres:ваш_надёжный_пароль@localhost:5432/lse_trading
```

Или создайте файл `.env` в корне lse:

```
POSTGRES_PASSWORD=ваш_пароль
```

---

## 3. Запуск PostgreSQL и восстановление дампа

```bash
cd ~/lse
docker compose up -d postgres
# Дождаться готовности (healthcheck)
sleep 10
chmod +x scripts/restore_pg_dump.sh
./scripts/restore_pg_dump.sh ~/lse_trading_dump_YYYYMMDD_HHMMSS.sql.gz
```

Если дамп лежит в другом месте, укажите полный путь:

```bash
./scripts/restore_pg_dump.sh /home/ai8049520/lse_trading_dump_20260316_120000.sql.gz
```

---

## 4. Запуск бота (контейнер LSE)

```bash
cd ~/lse
docker compose up -d
```

Проверка логов:

```bash
docker compose logs -f lse
```

По умолчанию контейнер работает в режиме **polling** (как раньше): бот сам опрашивает Telegram. Остановка: `docker compose stop lse`.

### 4.1 Переход на webhook (позже, для отладки)

Когда будете отлаживать webhook:

1. В `docker-compose.yml` у сервиса `lse` замените `command` на:
   ```yaml
   command: ["uvicorn", "api.bot_app:app", "--host", "0.0.0.0", "--port", "8080"]
   ```
   и добавьте:
   ```yaml
   ports:
     - "8080:8080"
   ```
2. Telegram принимает webhook **только по HTTPS**. Варианты: домен + nginx + certbot, либо туннель (cloudflared). Подробнее — в docs/DEPLOY_GCP.md (раздел про HTTPS).
3. После поднятия HTTPS: `python scripts/setup_webhook.py --url https://ваш-домен/webhook`. Проверка: `https://ваш-домен/webhook/info`.

---

## 5. Cron (задачи внутри контейнера)

На хосте ставим cron, который дергает скрипты в контейнере:

```bash
cd ~/lse
chmod +x setup_cron_docker.sh
./setup_cron_docker.sh
crontab -l
```

Имя контейнера по умолчанию — `lse-bot`. Если у вас другое, задайте перед установкой:

```bash
export LSE_CONTAINER_NAME=lse-bot
./setup_cron_docker.sh
```

Логи крона пишутся в `~/lse/logs/` на хосте (пути в crontab указывают на каталог проекта).

---

## 6. Опционально: веб-интерфейс

Сейчас в compose один сервис — бот. Если нужен веб (например `web_app.py` на порту 8000), можно:

- запускать его вручную на хосте (после `pip install -r requirements.txt` и с `DATABASE_URL` из config.env), или
- добавить в `docker-compose.yml` второй сервис с `command: ["python", "web_app.py"]` и портом 8000.

---

## 7. Краткий чеклист

| Шаг | Где | Действие |
|-----|-----|----------|
| 1 | Старый сервер | `./scripts/export_pg_dump.sh`, скопировать `.sql.gz` на новый хост |
| 2 | Новый сервер | Установить Docker + Compose, клонировать lse, создать `config.env` |
| 3 | Новый сервер | `docker compose up -d postgres`, затем `./scripts/restore_pg_dump.sh путь/к/дампу.sql.gz` |
| 4 | Новый сервер | `docker compose up -d` (бот), при необходимости настроить `POSTGRES_PASSWORD` / `.env` |
| 5 | Новый сервер | `./setup_cron_docker.sh` |

---

## 8. Обновление кода на новом сервере

Вручную:

```bash
cd ~/lse
git pull
docker compose build lse
docker compose up -d lse
```

Cron переустанавливать не нужно (пути к скриптам внутри контейнера те же).

### Ошибка сборки: no space left on device

При сборке образа `lse-bot` (зависимости `transformers`, `sentence-transformers`, `torch`) на диске может не хватить места. Ошибка вида: `write ... configuration_bit.cpython-311.pyc: no space left on device`.

**Что сделать на сервере:**

1. Освободить место под Docker и пересобрать:
   ```bash
   cd ~/lse
   chmod +x scripts/server_build_clean.sh
   ./scripts/server_build_clean.sh
   ```
   Скрипт останавливает контейнеры, делает `docker system prune -af` и `docker builder prune -af`, затем собирает образ `lse` с `--no-cache` и поднимает `docker compose up -d`.

2. Если места всё равно мало — проверить диск и при необходимости расширить или добавить второй диск (см. раздел 10):
   ```bash
   df -h /
   docker system df
   ```
   Для сборки образа с torch/transformers желательно иметь **не менее 5–6 ГБ** свободного места.

---

## 9. Автодеплой при изменениях на GitHub

Скрипт `scripts/deploy_from_github.sh` делает `git pull`, при появлении новых коммитов пересобирает образ и перезапускает контейнер `lse`.

### Запуск вручную

```bash
cd ~/lse
chmod +x scripts/deploy_from_github.sh
./scripts/deploy_from_github.sh          # деплой только при изменениях
./scripts/deploy_from_github.sh --force  # всегда пересобрать и перезапустить
```

### Cron (проверка каждые 10 минут)

На сервере:

```bash
crontab -e
```

Добавить строку (подставьте свой путь к репо и пользователю):

```
*/10 * * * * /home/ai8049520/lse/scripts/deploy_from_github.sh >> /home/ai8049520/lse/logs/deploy.log 2>&1
```

При необходимости задайте переменные перед вызовом скрипта (в crontab — через оболочку):

```bash
*/10 * * * * LSE_REPO_DIR=/home/ai8049520/lse /home/ai8049520/lse/scripts/deploy_from_github.sh >> /home/ai8049520/lse/logs/deploy.log 2>&1
```

Логи деплоя: `~/lse/logs/deploy.log`.

---

## 10. Второй диск (100 ГБ) для данных PostgreSQL

Если к VM добавлен отдельный диск (например 100 ГБ), его можно смонтировать и хранить на нём данные PostgreSQL, чтобы не забивать системный диск.

### 10.1 В GCP

- **Edit** инстанса → **Disks** → **Add new disk**: имя (например `disk-data`), **Size** 100 GB, **Zone** — та же, что у VM. **Save** → запустить VM.

### 10.2 На VM: формат, монтирование, автозагрузка

Подключись по SSH и выполни (имя устройства может быть `sdb` или другим — проверь `lsblk`):

```bash
# Узнать устройство нового диска (обычно sdb, если sda — загрузочный)
lsblk
# Если диск без разделов — например /dev/sdb

# Форматировать в ext4 (все данные на диске будут уничтожены)
sudo mkfs.ext4 -L lse-data /dev/sdb

# Каталог для монтирования
sudo mkdir -p /mnt/data

# Смонтировать
sudo mount /dev/sdb /mnt/data

# Добавить в fstab, чтобы монтировалось при загрузке (по UUID)
UUID=$(sudo blkid -s UUID -o value /dev/sdb)
echo "UUID=$UUID /mnt/data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
```

### 10.3 Каталог для PostgreSQL и права

```bash
sudo mkdir -p /mnt/data/pgdata
# postgres в образе pgvector обычно использует UID 999
sudo chown 999:999 /mnt/data/pgdata
```

### 10.4 Перенос данных и переключение compose

Если PostgreSQL уже запускался и в нём есть данные:

```bash
cd ~/lse
docker compose stop lse postgres
sudo cp -a /var/lib/docker/volumes/lse_lse_pgdata/_data/. /mnt/data/pgdata/
sudo chown -R 999:999 /mnt/data/pgdata
```

В `docker-compose.yml` заменить том postgres на примонтированный каталог:

**Было:**
```yaml
    volumes:
      - lse_pgdata:/var/lib/postgresql/data
```

**Станет:**
```yaml
    volumes:
      - /mnt/data/pgdata:/var/lib/postgresql/data
```

И в конце файла удалить или закомментировать в секции `volumes:` строку `lse_pgdata:` (если больше нигде не используется).

Запуск:

```bash
docker compose up -d
```

Если PostgreSQL ставится с нуля (дамп ещё не восстанавливали), достаточно создать `/mnt/data/pgdata`, поправить `docker-compose.yml` как выше и запустить `docker compose up -d`, затем выполнить `restore_pg_dump.sh`.

---

## 11. Чеклист «делаем вместе» (первый заход на VM)

SSH по ключам уже работает. Выполняй по шагам на сервере.

### Шаг 0: Клонировать репо и запустить скрипт настройки

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/kobyzev-yuri/lse.git ~/lse
cd ~/lse
chmod +x scripts/setup_server.sh scripts/deploy_from_github.sh scripts/restore_pg_dump.sh
```

Если на VM ещё **нет Docker**, запусти один раз:

```bash
./scripts/setup_server.sh
```

Скрипт установит Docker и напишет выйти из SSH и зайти снова. Сделай это (чтобы применилась группа `docker`).

### Шаг 1: Второй заход — конфиг и дамп

**config.env** и **дамп БД** в репозиторий не коммитить — копируй их на сервер напрямую (scp с ноутбука):

```bash
scp /path/to/lse/config.env ai8049520@IP_СЕРВЕРА:~/lse/
scp /path/to/my_backup.sql.gz ai8049520@IP_СЕРВЕРА:~/
```

Снова зайди по SSH. Если дамп уже лежит на сервере (например `~/my_backup.sql.gz`):

```bash
cd ~/lse
nano config.env   # вписать TELEGRAM_BOT_TOKEN и при необходимости OPENAI_API_KEY, TELEGRAM_SIGNAL_CHAT_ID и т.д.
./scripts/setup_server.sh ~/my_backup.sql.gz
```

Если дампа пока нет — запусти без аргумента (БД будет пустая), потом скопируешь дамп и выполнишь `./scripts/restore_pg_dump.sh ~/my_backup.sql.gz` отдельно.

### Шаг 2: Проверить, что бот и postgres работают

```bash
docker compose ps
docker compose logs -f lse
```

В логах не должно быть ошибок; бот в режиме polling.

### Шаг 3: Проверить скрипт обновления с GitHub

С ноутбука сделай маленький коммит и пуш в репо. На сервере:

```bash
cd ~/lse
./scripts/deploy_from_github.sh
```

В выводе должно быть «Changes detected», пересборка образа и перезапуск контейнера `lse`. Затем снова `docker compose logs -f lse` — бот должен работать с новым кодом.

#!/bin/bash
# Первоначальная настройка GCP VM для LSE: Docker, клон репо, postgres + бот.
# Запускать на сервере (после захода по SSH). Опционально: путь к дампу для восстановления.
#
# Использование:
#   ./scripts/setup_server.sh                    # без дампа (БД пустая)
#   ./scripts/setup_server.sh ~/my_backup.sql.gz # с восстановлением дампа
#
# Перед запуском: скопируй скрипт на сервер или клонируй репо и chmod +x scripts/setup_server.sh

set -e

REPO_DIR="${LSE_REPO_DIR:-$HOME/lse}"
DUMP_FILE="${1:-}"
GIT_REPO="${LSE_GIT_REPO:-https://github.com/kobyzev-yuri/lse.git}"

log() { echo "[setup] $*"; }

# --- 1. Docker ---
if ! command -v docker &>/dev/null; then
  log "Установка Docker..."
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ -f /etc/debian_version ]; then
    REPO_DISTRO="debian"
  else
    REPO_DISTRO="ubuntu"
  fi
  curl -fsSL "https://download.docker.com/linux/${REPO_DISTRO}/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  CODENAME=$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-unknown}" || echo "bookworm")
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${REPO_DISTRO} ${CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  log "Docker установлен. Выйди из SSH и зайди снова (чтобы применилась группа docker), затем запусти скрипт ещё раз или выполни вручную шаги 2–5 из docs/MIGRATE_SERVER.md"
  exit 0
fi

# --- 2. Репозиторий ---
if [ ! -d "$REPO_DIR/.git" ]; then
  log "Клонирование репозитория в $REPO_DIR ..."
  git clone "$GIT_REPO" "$REPO_DIR"
else
  log "Репозиторий уже есть: $REPO_DIR"
fi
cd "$REPO_DIR"

# --- 3. config.env ---
if [ ! -f config.env ]; then
  cp config.env.example config.env
  log "Создан config.env из шаблона. Обязательно отредактируй: nano config.env (TELEGRAM_BOT_TOKEN и др.)"
else
  log "config.env уже есть"
fi

# --- 4. Postgres ---
log "Запуск PostgreSQL..."
docker compose up -d postgres
log "Ожидание готовности Postgres (15 сек)..."
sleep 15

# --- 5. Восстановление дампа (если передан путь) ---
if [ -n "$DUMP_FILE" ] && [ -f "$DUMP_FILE" ]; then
  chmod +x scripts/restore_pg_dump.sh
  ./scripts/restore_pg_dump.sh "$DUMP_FILE"
elif [ -n "$DUMP_FILE" ]; then
  log "Файл дампа не найден: $DUMP_FILE (пропускаю восстановление)"
fi

# --- 6. Бот ---
log "Запуск контейнера бота..."
docker compose up -d

log "Готово. Проверка: docker compose ps && docker compose logs -f lse"
log "Обновление с GitHub вручную: ./scripts/deploy_from_github.sh"

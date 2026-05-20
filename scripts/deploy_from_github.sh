#!/bin/bash
# Автодеплой LSE на VM при изменениях в GitHub.
# Запускать на сервере из cron или по webhook. Требует: git, docker compose, репозиторий в LSE_REPO_DIR.
#
# Использование:
#   ./scripts/deploy_from_github.sh          # pull, при изменениях — rebuild и перезапуск lse
#   ./scripts/deploy_from_github.sh --force  # всегда пересобрать и перезапустить
#
# Cron (каждые 10 минут):
#   */10 * * * * /home/ai8049520/lse/scripts/deploy_from_github.sh >> /home/ai8049520/lse/logs/deploy.log 2>&1
#
# Опционально (мало RAM на VM: сборка + работающий контейнер lse):
#   LSE_STOP_BEFORE_BUILD=1 ./scripts/deploy_from_github.sh --force
# Подробный вывод шагов docker build (в deploy.log видно, что не «зависло»):
#   LSE_DEPLOY_BUILD_PLAIN=1 ./scripts/deploy_from_github.sh --force

set -e

REPO_DIR="${LSE_REPO_DIR:-$HOME/lse}"
# docker-compose service name (см. docker-compose.yml). По умолчанию это "lse" (container_name: lse-bot).
SERVICE_NAME="${LSE_SERVICE_NAME:-lse}"
CONTAINER_NAME="${LSE_CONTAINER_NAME:-lse-bot}"
LOG_DIR="${REPO_DIR}/logs"
FORCE=0

for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
    esac
done

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Deploy check started (repo=$REPO_DIR)"

if [ ! -d "$REPO_DIR/.git" ]; then
    log "ERROR: Not a git repo: $REPO_DIR"
    exit 2
fi

cd "$REPO_DIR"

# Сохраняем текущий коммит до pull
OLD_HEAD=$(git rev-parse HEAD 2>/dev/null || true)
log "git fetch + pull..."
git fetch origin
git pull --rebase --autostash || { log "ERROR: git pull failed"; exit 3; }
NEW_HEAD=$(git rev-parse HEAD 2>/dev/null || true)

if [ "$FORCE" -eq 1 ] || [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    log "Changes detected (or --force). Rebuilding and restarting service=$SERVICE_NAME (container=$CONTAINER_NAME)..."
    log "Commits: $OLD_HEAD -> $NEW_HEAD"
    log "Note: docker compose build can take many minutes (torch/pip); output may be sparse — see LSE_DEPLOY_BUILD_PLAIN in script header."
    export DOCKER_BUILDKIT=1
    BUILD_ARGS=(build "$SERVICE_NAME")
    if [ -n "${LSE_DEPLOY_BUILD_PLAIN:-}" ]; then
        BUILD_ARGS=(build --progress=plain "$SERVICE_NAME")
    fi
    if [ "${LSE_STOP_BEFORE_BUILD:-0}" = "1" ]; then
        log "LSE_STOP_BEFORE_BUILD=1: stopping service $SERVICE_NAME to free RAM for build (postgres stays up)..."
        docker compose stop "$SERVICE_NAME" 2>/dev/null || true
    fi
    log "Starting: docker compose ${BUILD_ARGS[*]}"
    time docker compose "${BUILD_ARGS[@]}"
    log "docker compose build finished."
    log "Starting: docker compose up -d $SERVICE_NAME"
    time docker compose up -d "$SERVICE_NAME"
    log "docker compose up finished."
    log "Deploy completed. Container: $CONTAINER_NAME"
else
    log "No changes. Skip rebuild. HEAD=$NEW_HEAD"
fi

exit 0

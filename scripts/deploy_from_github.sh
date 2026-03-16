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

set -e

REPO_DIR="${LSE_REPO_DIR:-$HOME/lse}"
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
git fetch origin
git pull --rebase --autostash || { log "ERROR: git pull failed"; exit 3; }
NEW_HEAD=$(git rev-parse HEAD 2>/dev/null || true)

if [ "$FORCE" -eq 1 ] || [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
    log "Changes detected (or --force). Rebuilding and restarting $CONTAINER_NAME..."
    docker compose build lse
    docker compose up -d lse
    log "Deploy completed. Container: $CONTAINER_NAME"
else
    log "No changes. Skip rebuild."
fi

exit 0

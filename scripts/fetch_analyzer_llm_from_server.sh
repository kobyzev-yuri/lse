#!/usr/bin/env bash
# Скачать на локальную машину JSON анализатора с LLM за N дней (тот же расчёт, что веб с галочкой LLM).
# Обходит WEB_DEMO_MODE: на сервере выполняется Python внутри контейнера, не HTTP /api/analyzer.
#
# Использование (с локальной машины):
#   chmod +x scripts/fetch_analyzer_llm_from_server.sh
#   ./scripts/fetch_analyzer_llm_from_server.sh
#
# Переменные окружения:
#   SSH_HOST   (по умолчанию ai8049520@104.154.205.58)
#   CONTAINER  (по умолчанию lse-bot)
#   DAYS       (по умолчанию 1)
#   STRATEGY   (по умолчанию GAME_5M)
#   OUT        (по умолчанию ./analyzer_GAME5M_llm_${DAYS}d_<timestamp>.json)
#
set -euo pipefail

SSH_HOST="${SSH_HOST:-ai8049520@104.154.205.58}"
CONTAINER="${CONTAINER:-lse-bot}"
DAYS="${DAYS:-1}"
STRATEGY="${STRATEGY:-GAME_5M}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT="${OUT:-./analyzer_${STRATEGY}_llm_${DAYS}d_${TS}.json}"

# JSON в stdout контейнера → перехват через SSH на локальный файл
ssh -o BatchMode=yes "${SSH_HOST}" \
  "docker exec ${CONTAINER} bash -lc 'cd /app && python3 scripts/export_analyzer_report.py --days ${DAYS} --strategy ${STRATEGY} --use-llm --include-trade-details'" \
  > "${OUT}"

echo "Saved: ${OUT}"

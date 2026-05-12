#!/usr/bin/env bash
# Запуск trade effectiveness analyzer внутри контейнера lse-bot (как export_analyzer_report.py).
# Предназначен для прода: зайти по SSH на ВМ, cd в репозиторий, выполнить этот скрипт.
#
# По умолчанию: 2 дня, GAME_5M, LLM включён, trade_effects в JSON.
#
# Примеры (на ВМ с docker; каталог на хосте не важен, главное — путь к скрипту):
#   ~/lse/scripts/run_analyzer_docker.sh
#   bash /path/to/lse/scripts/run_analyzer_docker.sh
#   DAYS=7 NO_LLM=1 ~/lse/scripts/run_analyzer_docker.sh
#   OUT=/home/ai8049520/reports/a.json ~/lse/scripts/run_analyzer_docker.sh
#
# Переменные окружения:
#   CONTAINER  — контейнер (по умолчанию lse-bot)
#   DAYS       — окно в днях (по умолчанию 2)
#   STRATEGY   — GAME_5M | ALL | … (по умолчанию GAME_5M)
#   OUT        — путь к JSON на хосте; если пусто — /tmp/analyzer_<STRATEGY>_llm_<DAYS>d_<UTC>.json
#   NO_LLM     — если 1/true — без LLM (--no-use-llm)
#   NO_TRADE_DETAILS — если 1/true — без массива trade_effects (легче файл)
#
set -euo pipefail

CONTAINER="${CONTAINER:-lse-bot}"
DAYS="${DAYS:-2}"
STRATEGY="${STRATEGY:-GAME_5M}"

TS="$(date -u +%Y%m%d_%H%M%SZ)"
LLM_TAG="llm"
if [[ "${NO_LLM:-0}" == "1" || "${NO_LLM:-}" == "true" ]]; then
  LLM_TAG="nollm"
fi
OUT="${OUT:-/tmp/analyzer_${STRATEGY}_${LLM_TAG}_${DAYS}d_${TS}.json}"

LLM_FLAG="--use-llm"
if [[ "${NO_LLM:-0}" == "1" || "${NO_LLM:-}" == "true" ]]; then
  LLM_FLAG="--no-use-llm"
fi

DETAILS_FLAG="--include-trade-details"
if [[ "${NO_TRADE_DETAILS:-0}" == "1" || "${NO_TRADE_DETAILS:-}" == "true" ]]; then
  DETAILS_FLAG="--no-include-trade-details"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker не найден в PATH" >&2
  exit 1
fi

echo "Container=${CONTAINER} DAYS=${DAYS} STRATEGY=${STRATEGY} OUT=${OUT}" >&2

# JSON пишем на хост через редирект (как scripts/fetch_analyzer_llm_from_server.sh)
docker exec "${CONTAINER}" bash -lc \
  "cd /app && python3 scripts/export_analyzer_report.py --days ${DAYS} --strategy ${STRATEGY} ${LLM_FLAG} ${DETAILS_FLAG}" \
  >"${OUT}"

echo "OK: ${OUT} ($(wc -c <"${OUT}" | tr -d ' ') bytes)" >&2

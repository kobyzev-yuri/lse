#!/usr/bin/env bash
# Запуск backtest_game5m_param_hypotheses.py в фоне (docker compose exec -d).
# Логи: docker compose logs -f lse  ИЛИ  --log-file внутри Python (том ./logs → /app/logs).
#
# Пример:
#   cd ~/lse && ./scripts/run_game5m_param_hypotheses_docker_bg.sh \
#     --log-file /app/logs/game5m_param_hypothesis_bg.log \
#     --json-out /app/logs/hanger_tune_open.json

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SERVICE="${LSE_COMPOSE_SERVICE:-lse}"

docker compose exec -d "$SERVICE" \
  env PYTHONUNBUFFERED=1 \
  python -u scripts/backtest_game5m_param_hypotheses.py "$@"
echo "Detached: сервис $SERVICE. Сводка в stdout контейнера: docker compose logs --tail=80 $SERVICE"
echo "Рекомендуется добавить: --log-file /app/logs/game5m_param_hypothesis_bg.log --json-out /app/logs/…"

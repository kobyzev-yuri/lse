#!/usr/bin/env bash
# Запуск backtest_game5m_param_hypotheses.py в фоне (docker compose exec -d).
# Остановка: ./scripts/stop_game5m_param_hypotheses_docker.sh  (или … -9 для SIGKILL).
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

# exec -d = только «Docker принял запуск»; python может сразу упасть — проверка ниже.
# Образ lse (python:3.11-slim) без procps — `ps` нет; проверяем /proc через python.
if ! docker compose exec -d -w /app "$SERVICE" \
  env PYTHONUNBUFFERED=1 \
  python -u scripts/backtest_game5m_param_hypotheses.py "$@"; then
  echo "Ошибка: docker compose exec -d не удалась (сервис $SERVICE не поднят или compose из другой директории?)." >&2
  exit 1
fi

echo "Detached: сервис $SERVICE (exec -d отправлен)."
echo "Сводка stdout/stderr основного процесса контейнера: docker compose logs --tail=80 $SERVICE"
if [[ "$*" != *--log-file* ]] || [[ "$*" != *--json-out* ]]; then
  echo "Подсказка: для долгого прогона удобно добавить --log-file /app/logs/….log --json-out /app/logs/….json" >&2
fi
echo "Проверка через 2 с: жив ли python (если пусто — смотрите лог без -d, см. ниже)…"
sleep 2
if out=$(
  docker compose exec -T -w /app "$SERVICE" python - <<'PY' 2>/dev/null
import os
import sys

needle = b"backtest_game5m_param_hypotheses"
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read()
    except (OSError, FileNotFoundError):
        continue
    if needle in cmd:
        line = cmd.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        print(f"pid={pid} {line}")
        sys.exit(0)
sys.exit(1)
PY
); then
  echo "Процесс найден:"
  echo "$out"
else
  echo "Внимание: процесс backtest_game5m_param_hypotheses в контейнере не найден — возможно сразу завершился." >&2
  echo "Диагностика без фона (увидите traceback в терминале):" >&2
  # shellcheck disable=SC2046
  echo "  docker compose exec -w /app $SERVICE env PYTHONUNBUFFERED=1 python -u scripts/backtest_game5m_param_hypotheses.py $(printf ' %q' "$@")" >&2
fi

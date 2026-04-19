#!/usr/bin/env bash
# Остановить фоновый scripts/backtest_game5m_param_hypotheses.py в контейнере lse.
# Образ slim без procps — ищем по /proc и шлём SIGTERM (при -9 — SIGKILL).

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
SERVICE="${LSE_COMPOSE_SERVICE:-lse}"
SIGNAL="${1:-TERM}"

sig=15
[[ "$SIGNAL" == "-9" || "$SIGNAL" == "KILL" || "$SIGNAL" == "9" ]] && sig=9 || true

docker compose exec -T -w /app "$SERVICE" python - "$sig" <<'PY'
import os
import signal
import sys

sig = int(sys.argv[1])
needle = b"backtest_game5m_param_hypotheses"
me = os.getpid()
killed: list[int] = []
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    pid = int(name)
    if pid == me:
        continue
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read()
    except (OSError, FileNotFoundError, ProcessLookupError):
        continue
    if needle not in cmd:
        continue
    try:
        os.kill(pid, sig)
        killed.append(pid)
    except ProcessLookupError:
        pass
    except PermissionError as e:
        print(f"pid {pid}: {e}", file=sys.stderr)

if killed:
    print("Остановлены PID:", killed)
else
    print("Процесс с backtest_game5m_param_hypotheses в cmdline не найден.")
PY

#!/usr/bin/env bash
# Минимальный venv для оффлайн-скриптов LSE (numpy, pandas, SQLAlchemy, psycopg2).
# Без полного requirements.txt (torch, transformers и т.д.).
#
# Примеры (через bash — работает без chmod +x):
#   bash scripts/bootstrap_min_venv.sh
#   bash scripts/bootstrap_min_venv.sh ~/nyse/.venv
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="${1:-$ROOT/.venv}"

if [[ -e "$VENV_PATH" ]]; then
  echo "Уже существует: $VENV_PATH — удалите вручную (rm -rf) или укажите другой путь." >&2
  exit 1
fi

echo "Создаю venv: $VENV_PATH"
python3 -m venv "$VENV_PATH"
"$VENV_PATH/bin/pip" install --upgrade pip
"$VENV_PATH/bin/pip" install --no-cache-dir -r "$ROOT/requirements-scripts-min.txt"
echo "Готово. Активация:"
echo "  source $VENV_PATH/bin/activate"
echo "Запуск из репозитория LSE:"
echo "  cd $ROOT && python3 scripts/cluster_portfolio_leaders.py"

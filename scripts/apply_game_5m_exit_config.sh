#!/usr/bin/env bash
# Merge config/game_5m_exit_recommended.env into config.env (local or GCP VM).
# Usage:
#   ./scripts/apply_game_5m_exit_config.sh
#   ./scripts/apply_game_5m_exit_config.sh --remote ai8049520@104.154.205.58:/home/ai8049520/lse/config.env

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRAG="${ROOT}/config/game_5m_exit_recommended.env"
TARGET="${ROOT}/config.env"

if [[ "${1:-}" == "--remote" && -n "${2:-}" ]]; then
  REMOTE="${2}"
  HOST="${REMOTE%%:*}"
  RPATH="${REMOTE#*:}"
  scp "$FRAG" "${HOST}:/tmp/game_5m_exit_recommended.env"
  ssh "$HOST" "set -euo pipefail
    cp -a '${RPATH}' '${RPATH}.bak.\$(date +%Y%m%d_%H%M%S)'
    while IFS= read -r line || [[ -n \"\$line\" ]]; do
      [[ \"\$line\" =~ ^[[:space:]]*# ]] && continue
      [[ -z \"\${line// }\" ]] && continue
      key=\"\${line%%=*}\"
      sed -i \"/^\${key}=/d\" '${RPATH}'
      echo \"\$line\" >> '${RPATH}'
    done < /tmp/game_5m_exit_recommended.env
    echo 'Updated keys in ${RPATH}'
    grep -E '^GAME_5M_(STALE|EARLY_DERISK|EXIT_ONLY)' '${RPATH}' | sort
  "
  exit 0
fi

if [[ ! -f "$TARGET" ]]; then
  echo "Missing ${TARGET}; copy from config.env.example first." >&2
  exit 1
fi
cp -a "$TARGET" "${TARGET}.bak.$(date +%Y%m%d_%H%M%S)"
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// }" ]] && continue
  key="${line%%=*}"
  sed -i "/^${key}=/d" "$TARGET"
  echo "$line" >> "$TARGET"
done < "$FRAG"
echo "Updated ${TARGET}"
grep -E '^GAME_5M_(STALE|EARLY_DERISK|EXIT_ONLY)' "$TARGET" | sort

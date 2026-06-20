#!/usr/bin/env bash
# Verify lse-bot image excludes dev-only paths (see docs/RUNTIME_SLIM_DEPLOY_PLAN.md).
# Usage:
#   ./scripts/verify_docker_slim.sh              # check running container
#   ./scripts/verify_docker_slim.sh --build      # build image then check
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${LSE_CONTAINER_NAME:-lse-bot}"
IMAGE="${LSE_IMAGE:-lse-bot:latest}"
DO_BUILD=0

for arg in "$@"; do
  case "$arg" in
    --build) DO_BUILD=1 ;;
    -h|--help)
      echo "Usage: $0 [--build]"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

ok() {
  echo "OK: $*"
}

if [ "$DO_BUILD" -eq 1 ]; then
  echo "Building image $IMAGE ..."
  (cd "$ROOT" && docker compose build lse)
fi

if ! docker ps -q -f "name=^${CONTAINER}$" | grep -q .; then
  echo "Container $CONTAINER not running; checking image filesystem via temporary container ..."
  docker run --rm --entrypoint sh "$IMAGE" -c '
    test ! -d /app/tests
    test ! -d /app/docs
    test ! -d /app/scripts/archive
    test -f /app/templates/earnings_ui_guide.md
  ' || fail "slim checks failed on image $IMAGE"
  ok "image $IMAGE passes slim checks"
  exit 0
fi

docker exec "$CONTAINER" test ! -d /app/tests || fail "/app/tests must not exist"
docker exec "$CONTAINER" test ! -d /app/docs || fail "/app/docs must not exist"
docker exec "$CONTAINER" test ! -d /app/scripts/archive || fail "/app/scripts/archive must not exist"
docker exec "$CONTAINER" test -f /app/templates/earnings_ui_guide.md || fail "missing /app/templates/earnings_ui_guide.md"

ok "container $CONTAINER passes slim checks"

#!/usr/bin/env bash
# Post-deploy tests: copy tests/ from host git tree into lse-bot (slim image has no tests/).
#
# Usage (on VM, after deploy_from_github.sh):
#   ./scripts/post_deploy_tests.sh
#   LSE_POST_DEPLOY_TEST_FILES=tests/test_options_tools.py ./scripts/post_deploy_tests.sh
#
# Env:
#   LSE_CONTAINER_NAME   default lse-bot
#   LSE_REPO_DIR         default $HOME/lse
#   LSE_POST_DEPLOY_TEST_FILES  space-separated paths under repo (default: tests/test_options_tools.py)

set -euo pipefail

REPO_DIR="${LSE_REPO_DIR:-$HOME/lse}"
CONTAINER="${LSE_CONTAINER_NAME:-lse-bot}"
TEST_FILES="${LSE_POST_DEPLOY_TEST_FILES:-tests/test_options_tools.py tests/test_decision_stack_game5m.py}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] post_deploy_tests: $*"; }

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    log "ERROR: container $CONTAINER not found"
    exit 2
fi

if [ ! -d "$REPO_DIR/tests" ]; then
    log "ERROR: no tests/ in $REPO_DIR"
    exit 2
fi

log "docker cp tests/ -> $CONTAINER:/app/tests"
docker cp "$REPO_DIR/tests" "$CONTAINER:/app/tests"

log "install pytest in container (if missing)"
docker exec "$CONTAINER" python3 -m pip install -q pytest 2>/dev/null || \
    docker exec "$CONTAINER" pip install -q pytest

FAILED=0
if [ "$TEST_FILES" = "tests/" ] || [ "$TEST_FILES" = "tests" ]; then
    log "pytest tests/ (full suite)"
    if ! docker exec "$CONTAINER" python3 -m pytest tests/ -q --tb=line; then
        FAILED=1
    fi
else
    for rel in $TEST_FILES; do
        host_path="$REPO_DIR/$rel"
        if [ ! -f "$host_path" ]; then
            log "WARN: skip missing $rel"
            continue
        fi
        log "pytest $rel"
        if ! docker exec "$CONTAINER" python3 -m pytest "$rel" -q --tb=line; then
            FAILED=1
        fi
    done
fi

if [ "${LSE_POST_DEPLOY_TESTS_CLEANUP:-1}" = "1" ]; then
    log "remove /app/tests from container (slim runtime)"
    docker exec "$CONTAINER" rm -rf /app/tests
fi

if [ "$FAILED" -ne 0 ]; then
    log "FAILED: one or more test files failed"
    exit 1
fi

log "OK"
exit 0

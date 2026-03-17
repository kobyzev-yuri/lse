#!/usr/bin/env bash
# На сервере: освободить место и пересобрать образ lse-bot.
# Запуск: bash scripts/server_build_clean.sh
set -e
echo "=== Место до очистки ==="
df -h /
docker system df 2>/dev/null || true

echo ""
echo "=== Очистка Docker (образы, контейнеры, build cache) ==="
docker compose down 2>/dev/null || true
docker system prune -af
docker builder prune -af

echo ""
echo "=== Место после очистки ==="
df -h /
docker system df 2>/dev/null || true

echo ""
echo "=== Сборка lse-bot (--no-cache) ==="
docker compose build --no-cache lse

echo ""
echo "=== Запуск ==="
docker compose up -d

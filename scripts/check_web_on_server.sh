#!/usr/bin/env bash
# На сервере: проверка, что веб на 8080 поднят и отвечает.
# Запуск: bash scripts/check_web_on_server.sh
set -e
echo "=== 1. Команда контейнера lse (должны быть uvicorn и web_app) ==="
docker inspect lse-bot --format '{{.Config.Cmd}}' 2>/dev/null || echo "Контейнер lse-bot не найден"

echo ""
echo "=== 2. Порт 8080 на хосте (должен быть 0.0.0.0:8080) ==="
docker port lse-bot 8080 2>/dev/null || echo "Порт 8080 не проброшен"

echo ""
echo "=== 3. Последние 30 строк логов (ищем Uvicorn, ошибки) ==="
docker compose -f ~/lse/docker-compose.yml logs --tail 30 lse 2>/dev/null || docker logs --tail 30 lse-bot 2>/dev/null

echo ""
echo "=== 4. Проверка с хоста: curl 127.0.0.1:8080 ==="
curl -s -o /dev/null -w "HTTP %{http_code}\n" --connect-timeout 3 http://127.0.0.1:8080/ || echo "Не удалось подключиться к 127.0.0.1:8080"

echo ""
echo "=== 5. Проверка /game5m/cards ==="
curl -s -o /dev/null -w "HTTP %{http_code}\n" --connect-timeout 3 http://127.0.0.1:8080/game5m/cards || echo "Не удалось подключиться"

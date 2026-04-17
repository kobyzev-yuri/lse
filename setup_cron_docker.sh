#!/bin/bash
# Cron для LSE, когда приложение запущено в Docker (docker-compose).
# Задачи вызывают скрипты внутри контейнера lse-bot.
# Запуск: ./setup_cron_docker.sh (из корня проекта).

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_NAME="${LSE_CONTAINER_NAME:-lse-bot}"

# Проверяем, что контейнер есть (может быть остановлен — для cron он будет запускаться по расписанию или нужен всегда up для бота)
if ! docker ps -a -q -f name="^${CONTAINER_NAME}$" 2>/dev/null | grep -q .; then
  echo "⚠️  Контейнер $CONTAINER_NAME не найден. Сначала: docker-compose up -d"
  exit 1
fi

mkdir -p "$PROJECT_DIR/logs"
CRON_FILE=$(mktemp)

# Удаляем старые LSE docker cron-задачи
crontab -l 2>/dev/null | grep -v "docker.*lse-bot\|LSE.*Docker\|send_sndk_signal_cron\|trading_cycle_cron\|premarket_cron\|update_prices_cron\|fetch_news_cron\|sync_vector_kb_cron\|add_sentiment_to_news\|analyze_event_outcomes\|cleanup_calendar_noise\|cron_watchdog\|update_rsi_local\|update_finviz\|ingest_market_bars_intraday" | grep -v "$PROJECT_DIR" > "$CRON_FILE" || true
if ! grep -q . "$CRON_FILE" 2>/dev/null; then
  : > "$CRON_FILE"
fi

cat >> "$CRON_FILE" << EOF

# ========== LSE Trading System (Docker) ==========
# Контейнер: $CONTAINER_NAME

# Цены: будни 10,12,14,16,18,20,22 ч; выходные только 22 ч
0 10,12,14,16,18,20,22 * * 1-5 docker exec $CONTAINER_NAME python scripts/update_prices_cron.py >> /dev/null 2>&1
0 22 * * 0,6 docker exec $CONTAINER_NAME python scripts/update_prices_cron.py >> /dev/null 2>&1
10 22 * * * docker exec $CONTAINER_NAME python scripts/update_rsi_local.py >> /dev/null 2>&1
0 19 * * 1-5 docker exec $CONTAINER_NAME python update_finviz_data.py >> /dev/null 2>&1
0 9,13,17 * * 1-5 docker exec $CONTAINER_NAME python scripts/trading_cycle_cron.py >> "$PROJECT_DIR/logs/cron_trading_cycle.log" 2>&1
*/5 * * * 1-5 docker exec $CONTAINER_NAME python scripts/send_sndk_signal_cron.py >> "$PROJECT_DIR/logs/cron_sndk_signal.log" 2>&1
15 17 * * 1-5 docker exec $CONTAINER_NAME python scripts/premarket_cron.py >> "$PROJECT_DIR/logs/premarket_cron.log" 2>&1
# Новости core-fast: каждые 15 мин (RSS + Alpha Vantage), без параллельных запусков
*/15 * * * * flock -n /tmp/lse_news_core_fast.lock docker exec $CONTAINER_NAME python scripts/fetch_news_cron.py --mode core-fast >> "$PROJECT_DIR/logs/news_fetch.log" 2>&1
# Новости NewsAPI: раз в 2 часа (отдельно, чтобы backoff не тормозил другие источники), без параллельных запусков
10 */2 * * * flock -n /tmp/lse_news_newsapi.lock docker exec $CONTAINER_NAME python scripts/fetch_news_cron.py --mode newsapi >> "$PROJECT_DIR/logs/news_fetch.log" 2>&1
# Новости Investing: раз в 2 часа (снижение 429), без параллельных запусков
0 */2 * * * flock -n /tmp/lse_news_investing.lock docker exec $CONTAINER_NAME python scripts/fetch_news_cron.py --mode investing >> "$PROJECT_DIR/logs/news_fetch.log" 2>&1
10 * * * * docker exec $CONTAINER_NAME python scripts/sync_vector_kb_cron.py >> "$PROJECT_DIR/logs/sync_vector_kb.log" 2>&1
20 * * * * docker exec $CONTAINER_NAME python scripts/add_sentiment_to_news_cron.py >> "$PROJECT_DIR/logs/add_sentiment_to_news.log" 2>&1
0 4 * * * docker exec $CONTAINER_NAME python scripts/analyze_event_outcomes_cron.py >> "$PROJECT_DIR/logs/analyze_event_outcomes.log" 2>&1
30 4 * * * docker exec $CONTAINER_NAME python scripts/cleanup_calendar_noise.py --execute >> "$PROJECT_DIR/logs/cleanup_calendar_noise.log" 2>&1
45 * * * * docker exec $CONTAINER_NAME python scripts/cron_watchdog.py --execute >> "$PROJECT_DIR/logs/cron_watchdog.log" 2>&1
# 5m/30m в Postgres для бэктеста (Yahoo, UPSERT). Ежедневно 23:25 по системному TZ сервера (ожидается MSK).
25 23 * * * flock -n /tmp/lse_market_bars_intraday.lock docker exec $CONTAINER_NAME python scripts/ingest_market_bars_intraday.py >> "$PROJECT_DIR/logs/cron_market_bars_intraday.log" 2>&1
EOF

crontab "$CRON_FILE"
rm -f "$CRON_FILE"
echo "✅ Cron для Docker установлен (контейнер: $CONTAINER_NAME). Проверка: crontab -l"
echo "   В т.ч. ingest_market_bars_intraday: ежедневно 23:25 → logs/cron_market_bars_intraday.log (таблицы: migrate_market_bars_intraday.py один раз)"

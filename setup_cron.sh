#!/bin/bash
# Скрипт для настройки cron задач для LSE Trading System
# Запуск: ./setup_cron.sh (из корня проекта)
# Используется conda env py11; пути подставляются автоматически.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Явно используем conda env py11 (проект переведён с py310 на py11)
if PY11_PATH=$(conda run -n py11 which python 2>/dev/null); then
    PYTHON_PATH="$PY11_PATH"
elif [ -n "$CONDA_PREFIX" ] && [[ "$CONDA_PREFIX" == *py11* ]]; then
    PYTHON_PATH="$CONDA_PREFIX/bin/python"
else
    for base in "$HOME/anaconda3" "$HOME/miniconda3" "/mnt/ai/src/anaconda3" "/vol/src/anaconda3"; do
        [ ! -d "$base" ] && continue
        for exe in "$base/envs/py11/bin/python" "$base/envs/py11/bin/python3"; do
            if [ -x "$exe" ]; then
                PYTHON_PATH="$exe"
                break 2
            fi
        done
    done
fi
[ -z "$PYTHON_PATH" ] && PYTHON_PATH=$(which python3)

echo "Настройка cron задач для LSE Trading System"
echo "Проект: $PROJECT_DIR"
echo "Python (py11): $PYTHON_PATH"

# Создаем директорию для логов
mkdir -p "$PROJECT_DIR/logs"

# Временный файл для нового crontab
CRON_FILE=$(mktemp)

# Удаляем из текущего crontab ВСЕ задачи и комментарии LSE, чтобы не оставались дубликаты или старые пути.
REMOVE_PATTERNS="LSE Trading System|========== LSE|Проект:.*lse|update_prices_cron\.py|trading_cycle_cron\.py|fetch_news_cron\.py|sync_vector_kb_cron\.py|add_sentiment_to_news_cron\.py|analyze_event_outcomes_cron\.py|cleanup_calendar_noise\.py|update_rsi_local\.py|update_finviz_data\.py|update_finviz\.py|send_sndk_signal_cron\.py|premarket_cron\.py|cron_watchdog\.py"
REMOVE_COMMENTS="Проект:|Обновление цен:|Локальный RSI|валюты/товары|вечернего обновления цен|RSI с Finviz|после дневной сессии|Торговый цикл|9:00, 13:00, 17:00|Новости \(RSS|Alpha Vantage\).*каждый час|Backfill embedding|Sentiment к новостям|Анализ исходов событий|Обновление RSI ежедневно|после обновления цен|после закрытия всех бирж|NYSE \(00:00 MSK\)|измените время|Если сервер не в MSK"
crontab -l 2>/dev/null | grep -vE "$REMOVE_PATTERNS|$REMOVE_COMMENTS" | grep -v "$PROJECT_DIR" | grep -v "/mnt/ai/cnn/lse" > "$CRON_FILE" || true
# Если crontab был пустой или только наши задачи — файл может быть пустым; тогда начинаем с пустого
if ! grep -q . "$CRON_FILE" 2>/dev/null; then
    : > "$CRON_FILE"
fi

# Добавляем единый блок задач LSE (проект + conda env py11)
cat >> "$CRON_FILE" << EOF

# ========== LSE Trading System ==========
# Проект: $PROJECT_DIR
# Python: conda env py11

# Обновление цен: ежедневно в 22:00 MSK (после закрытия бирж) + в сессию для актуальности (пн-пт)
0 22 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/update_prices_cron.py >> logs/cron_update_prices.log 2>&1
0 10,12,14,16,18,20 * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH scripts/update_prices_cron.py >> logs/cron_update_prices.log 2>&1

# Локальный RSI (валюты/товары): через 10 мин после вечернего обновления цен
10 22 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/update_rsi_local.py >> logs/update_rsi_local.log 2>&1

# RSI с Finviz (акции): пн-пт в 19:00 (после дневной сессии)
0 19 * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH update_finviz_data.py >> logs/update_finviz.log 2>&1

# Портфельная игра: тикеры из config.env (TRADING_CYCLE_TICKERS или TICKERS_MEDIUM+TICKERS_LONG). Аргумент переопределяет: ... trading_cycle_cron.py "MSFT,ORCL"
0 9,13,17 * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH scripts/trading_cycle_cron.py >> logs/cron_trading_cycle.log 2>&1

# Игра 5m: тикеры из config.env TICKERS_FAST. Аргумент переопределяет: ... send_sndk_signal_cron.py "SNDK,NDK"
*/5 * * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH scripts/send_sndk_signal_cron.py >> logs/cron_sndk_signal.log 2>&1

# Премаркет: за 15 мин до открытия US (9:15 ET = 17:15 MSK зимой). При PREMARKET_ALERT_TELEGRAM=true — алерт в Telegram. Летом (EDT): 9:15 ET = 16:15 MSK — при необходимости сменить на 15 16
15 17 * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH scripts/premarket_cron.py >> logs/premarket_cron.log 2>&1

# Новости core-fast (RSS + Alpha Vantage): каждые 15 мин
*/15 * * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/fetch_news_cron.py --mode core-fast >> logs/news_fetch.log 2>&1
# Новости NewsAPI: раз в 2 часа (снижение 429 и долгих backoff)
10 */2 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/fetch_news_cron.py --mode newsapi >> logs/news_fetch.log 2>&1
# Новости Investing (calendar + news): раз в 2 часа (снижение 429 Too Many Requests)
0 */2 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/fetch_news_cron.py --mode investing >> logs/news_fetch.log 2>&1

# Backfill embedding в knowledge_base (после сбора новостей; без прокси внутри скрипта)
10 * * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/sync_vector_kb_cron.py >> logs/sync_vector_kb.log 2>&1

# Sentiment и insight для новостей без sentiment (нужен USE_LLM=true в config.env)
20 * * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/add_sentiment_to_news_cron.py >> logs/add_sentiment_to_news.log 2>&1

# Анализ исходов событий (outcome_json): раз в день, событиям нужно 7+ дней истории котировок
0 4 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/analyze_event_outcomes_cron.py >> logs/analyze_event_outcomes.log 2>&1

# Очистка мусора в knowledge_base (Alpha Vantage Earnings «Earnings report for X», ECONOMIC_INDICATOR-шум)
30 4 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/cleanup_calendar_noise.py --execute >> logs/cleanup_calendar_noise.log 2>&1

# Watchdog: сканирование логов cron на ошибки, запись в logs/cron_watchdog.log
45 * * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/cron_watchdog.py --execute >> logs/cron_watchdog.log 2>&1
EOF

# Устанавливаем новый crontab
crontab "$CRON_FILE"
rm -f "$CRON_FILE"

echo "✅ Cron задачи установлены (проект: $PROJECT_DIR):"
echo "  - Обновление цен: ежедневно 22:00; в сессию пн-пт в 10,12,14,16,18,20"
echo "  - RSI локальный: 22:10 (после обновления цен)"
echo "  - RSI Finviz: пн-пт 19:00"
echo "  - Торговый цикл: пн-пт 9:00, 13:00, 17:00"
echo "  - Игра 5m (сигнал): каждые 5 мин пн-пт (при закрытой бирже — выход без 5m)"
echo "  - Премаркет: пн-пт 17:15 MSK, за 15 мин до открытия US (алерт при PREMARKET_ALERT_TELEGRAM=true)"
echo "  - Новости core-fast (RSS/AlphaVantage): каждые 15 мин"
echo "  - Новости NewsAPI: раз в 2 часа (в 10 минут)"
echo "  - Новости Investing (calendar+news): раз в 2 часа"
echo "  - Backfill embedding: каждый час в :10 (после новостей)"
echo "  - Sentiment к новостям: каждый час в :20 (при USE_LLM=true)"
echo "  - Анализ исходов событий (outcome_json): ежедневно в 4:00"
echo "  - Очистка мусора новостей (cleanup_calendar_noise): ежедневно в 4:30"
echo "  - Watchdog логов cron (ошибки → logs/cron_watchdog.log): каждый час в :45"
echo ""
echo "⚠️  Часовой пояс: cron использует системный (проверка: timedatectl | grep 'Time zone')."
echo "   Для MSK убедитесь, что сервер в Europe/Moscow или подстройте часы в crontab -e"
echo ""
echo "  Просмотр:   crontab -l"
echo "  Редактирование: crontab -e"


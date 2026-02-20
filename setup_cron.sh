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
REMOVE_PATTERNS="LSE Trading System|========== LSE|Проект:.*lse|update_prices_cron\.py|trading_cycle_cron\.py|fetch_news_cron\.py|update_rsi_local\.py|update_finviz_data\.py|update_finviz\.py"
REMOVE_COMMENTS="Проект:|Обновление цен:|Локальный RSI|валюты/товары|вечернего обновления цен|RSI с Finviz|после дневной сессии|Торговый цикл|9:00, 13:00, 17:00|Новости \(RSS|Alpha Vantage\).*каждый час|Обновление RSI ежедневно|после обновления цен|после закрытия всех бирж|NYSE \(00:00 MSK\)|измените время|Если сервер не в MSK"
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

# Торговый цикл: 9:00, 13:00, 17:00 MSK (пн-пт)
0 9,13,17 * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH scripts/trading_cycle_cron.py >> logs/cron_trading_cycle.log 2>&1

# Новости (RSS, NewsAPI, Alpha Vantage): каждый час
0 * * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/fetch_news_cron.py >> logs/news_fetch.log 2>&1
EOF

# Устанавливаем новый crontab
crontab "$CRON_FILE"
rm -f "$CRON_FILE"

echo "✅ Cron задачи установлены (проект: $PROJECT_DIR):"
echo "  - Обновление цен: ежедневно 22:00; в сессию пн-пт в 10,12,14,16,18,20"
echo "  - RSI локальный: 22:10 (после обновления цен)"
echo "  - RSI Finviz: пн-пт 19:00"
echo "  - Торговый цикл: пн-пт 9:00, 13:00, 17:00"
echo "  - Новости: каждый час (:00)"
echo ""
echo "⚠️  Часовой пояс: cron использует системный (проверка: timedatectl | grep 'Time zone')."
echo "   Для MSK убедитесь, что сервер в Europe/Moscow или подстройте часы в crontab -e"
echo ""
echo "  Просмотр:   crontab -l"
echo "  Редактирование: crontab -e"


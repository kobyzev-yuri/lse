#!/bin/bash
# Скрипт для настройки cron задач для LSE Trading System

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_PATH=$(which python3)

echo "Настройка cron задач для LSE Trading System"
echo "Проект: $PROJECT_DIR"
echo "Python: $PYTHON_PATH"

# Создаем директорию для логов
mkdir -p "$PROJECT_DIR/logs"

# Создаем временный файл с cron задачами
CRON_FILE=$(mktemp)

# Получаем существующие cron задачи (исключая наши)
crontab -l 2>/dev/null | grep -v "LSE Trading System" | grep -v "$PROJECT_DIR" > "$CRON_FILE"

# Добавляем новые задачи
cat >> "$CRON_FILE" << EOF

# LSE Trading System - автоматическое обновление цен (ежедневно в 18:00)
0 18 * * * cd $PROJECT_DIR && $PYTHON_PATH scripts/update_prices_cron.py >> logs/cron_update_prices.log 2>&1

# LSE Trading System - торговый цикл (каждые 4 часа в рабочее время: 9:00, 13:00, 17:00)
0 9,13,17 * * 1-5 cd $PROJECT_DIR && $PYTHON_PATH scripts/trading_cycle_cron.py >> logs/cron_trading_cycle.log 2>&1
EOF

# Устанавливаем новые cron задачи
crontab "$CRON_FILE"
rm "$CRON_FILE"

echo "✅ Cron задачи установлены:"
echo "  - Обновление цен: ежедневно в 18:00"
echo "  - Торговый цикл: в 9:00, 13:00, 17:00 (пн-пт)"
echo ""
echo "Просмотр задач: crontab -l"
echo "Редактирование: crontab -e"


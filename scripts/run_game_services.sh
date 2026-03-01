#!/bin/bash
# Подготовка и запуск сервисов для игры 5m (тикеры из TICKERS_FAST в config.env).
# Запуск из корня проекта: ./scripts/run_game_services.sh
# Опции: --bot-only   только бот (без cron и веба)
#        --no-bot     не запускать бот (только подготовка + cron)
# Загрузите config.env или задайте TICKERS_FAST (например SNDK,MU) перед запуском.

set -e
cd "$(dirname "$0")/.."
PROJECT_DIR="$PWD"

# Тикеры для игры 5m: из окружения или fallback для совместимости
export TICKERS_FAST="${TICKERS_FAST:-SNDK}"

echo "=============================================="
echo "  Подготовка сервисов игры 5m (TICKERS_FAST)"
echo "  TICKERS_FAST=$TICKERS_FAST"
echo "=============================================="

# 1. Проверка config
if [ ! -f config.env ]; then
    echo "❌ config.env не найден. Скопируйте config.env.example в config.env и заполните TELEGRAM_BOT_TOKEN, DATABASE_URL, TELEGRAM_SIGNAL_CHAT_ID (или TELEGRAM_SIGNAL_CHAT_IDS)."
    exit 1
fi
if ! grep -q "TELEGRAM_BOT_TOKEN=.\+" config.env 2>/dev/null; then
    echo "⚠️  В config.env задайте TELEGRAM_BOT_TOKEN"
fi
echo "✅ config.env найден"

# 2. Инициализация БД
echo ""
echo "Инициализация БД..."
python init_db.py || { echo "❌ init_db.py failed"; exit 1; }
echo "✅ БД готова"

# 3. Проверка 5m по SNDK (не критично, только информация)
echo ""
echo "Проверка 5m данных по SNDK..."
python scripts/check_fast_tickers_5m.py || true

# 4. Cron (пропуск при --bot-only)
if [[ " $* " != *" --bot-only "* ]]; then
    echo ""
    echo "Установка cron (рассылка сигналов каждые 5 мин в торговые часы)..."
    if [ -f setup_cron.sh ]; then
        ./setup_cron.sh
        echo "✅ Cron установлен"
    else
        echo "⚠️  setup_cron.sh не найден, установите cron вручную: crontab -e"
    fi
fi

# 5. Telegram бот
if [[ " $* " != *" --no-bot "* ]]; then
    echo ""
    echo "Запуск Telegram бота (polling)..."
    echo "  Остановка: Ctrl+C"
    echo "  Логи: смотрите вывод ниже или перенаправьте в logs/telegram_bot.log"
    echo ""
    exec python scripts/run_telegram_bot.py
fi

echo ""
echo "Готово. Запустите бота вручную: python scripts/run_telegram_bot.py"

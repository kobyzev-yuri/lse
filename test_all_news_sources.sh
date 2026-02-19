#!/bin/bash
# Скрипт для тестирования всех источников новостей
# Использует conda env py11 (Python 3.11) — feedparser ставится без ошибок

cd /home/cnn/lse

# Активируем conda окружение py11
if command -v conda &>/dev/null; then
  eval "$(conda shell.bash hook)"
  conda activate py11 2>/dev/null || true
fi

echo "=========================================="
echo "🧪 Тестирование источников новостей"
echo "=========================================="
echo "Python: $(which python3) ($(python3 --version 2>/dev/null))"
echo ""

echo "1️⃣ Тест RSS фидов центральных банков..."
python3 services/rss_news_fetcher.py
echo ""

echo "2️⃣ Тест Investing.com Economic Calendar..."
python3 services/investing_calendar_parser.py
echo ""

echo "3️⃣ Тест NewsAPI..."
python3 services/newsapi_fetcher.py
echo ""

echo "4️⃣ Тест Alpha Vantage..."
python3 services/alphavantage_fetcher.py
echo ""

echo "=========================================="
echo "✅ Все тесты завершены"
echo "=========================================="

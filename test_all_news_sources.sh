#!/bin/bash
# Скрипт для тестирования всех источников новостей

cd /home/cnn/lse

echo "=========================================="
echo "🧪 Тестирование источников новостей"
echo "=========================================="
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

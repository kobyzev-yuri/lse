# Установка зависимостей для новостных скриптов

## Быстрая установка

```bash
cd /home/cnn/lse
pip install feedparser>=6.0.10 lxml>=4.9.0
```

Или установить все зависимости из requirements.txt:

```bash
pip install -r requirements.txt
```

## Проверка установки

```bash
python3 -c "import feedparser; print('feedparser OK')"
python3 -c "import lxml; print('lxml OK')"
```

## Запуск скриптов

После установки зависимостей можно запускать скрипты:

```bash
# Из корня проекта
cd /home/cnn/lse

# По одному
python3 services/rss_news_fetcher.py
python3 services/investing_calendar_parser.py
python3 services/newsapi_fetcher.py
python3 services/alphavantage_fetcher.py

# Или все сразу
python3 scripts/fetch_news_cron.py
```

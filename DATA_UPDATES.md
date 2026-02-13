# Руководство по обновлению данных

## Обновление цен котировок

### Автоматическое обновление

#### Вариант 1: Использование cron (Linux/Mac)

Добавьте в crontab для ежедневного обновления в 18:00 (после закрытия рынка):

```bash
# Открыть crontab
crontab -e

# Добавить строку (замените /path/to/lse на ваш путь)
0 18 * * 1-5 cd /mnt/ai/cnn/lse && /usr/bin/python3 update_prices.py >> /tmp/lse_update.log 2>&1
```

Это будет обновлять данные каждый рабочий день в 18:00.

#### Вариант 2: Использование systemd timer (Linux)

Создайте файл `/etc/systemd/system/lse-update-prices.service`:

```ini
[Unit]
Description=LSE Trading System - Update Prices
After=network.target

[Service]
Type=oneshot
User=your_user
WorkingDirectory=/mnt/ai/cnn/lse
ExecStart=/usr/bin/python3 /mnt/ai/cnn/lse/update_prices.py
StandardOutput=journal
StandardError=journal
```

И файл `/etc/systemd/system/lse-update-prices.timer`:

```ini
[Unit]
Description=Run LSE price update daily
Requires=lse-update-prices.service

[Timer]
OnCalendar=Mon-Fri 18:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Активируйте таймер:
```bash
sudo systemctl enable lse-update-prices.timer
sudo systemctl start lse-update-prices.timer
```

#### Вариант 3: Использование Python schedule (для тестирования)

Создайте файл `scheduler.py`:

```python
import schedule
import time
from update_prices import update_all_prices

# Обновление каждый день в 18:00
schedule.every().day.at("18:00").do(update_all_prices)

# Или каждые 4 часа в рабочее время
# schedule.every(4).hours.do(update_all_prices)

while True:
    schedule.run_pending()
    time.sleep(60)
```

Запуск: `python scheduler.py`

### Ручное обновление

```bash
# Обновить все тикеры из БД
python update_prices.py

# Обновить конкретные тикеры
python update_prices.py MSFT,SNDK,GBPUSD=X
```

**Примечание о формате тикеров:**
- **Акции**: обычный формат (MSFT, SNDK, AAPL)
- **Валютные пары**: формат Yahoo Finance с суффиксом `=X`:
  - `GBPUSD=X` - британский фунт к доллару США (GBP/USD)
  - `EURUSD=X` - евро к доллару США (EUR/USD)
  - `USDJPY=X` - доллар США к японской иене (USD/JPY)
  
  Формат: `[BASE][QUOTE]=X`, где BASE - базовая валюта, QUOTE - котируемая валюта.
  Суффикс `=X` указывает Yahoo Finance, что это валютная пара, а не акция.

### Что делает скрипт

1. Определяет список тикеров из таблицы `quotes`
2. Для каждого тикера:
   - Проверяет дату последнего обновления
   - Загружает новые данные из yfinance (с последней даты)
   - Рассчитывает SMA_5 и volatility_5
   - Вставляет только новые записи (ON CONFLICT DO NOTHING)

---

## Добавление новостей

### Интерактивное добавление

```bash
python news_importer.py add
```

Скрипт попросит ввести:
- Тикер (или MACRO/US_MACRO для макро-новостей)
- Источник новости
- Текст новости
- Sentiment score (опционально)

### Импорт из CSV

Создайте файл `news.csv`:

```csv
ticker,source,content,sentiment_score,ts
MACRO,BLS Release,"US CPI January 2.4% (Lower than expected). Bullish for Tech, Bearish for USD.",0.75,2026-02-13 08:30:00
MSFT,Reuters,"Microsoft reports strong earnings, cloud revenue up 25%.",0.85,2026-02-13 10:00:00
```

Импорт:
```bash
python news_importer.py import news.csv
```

### Импорт из JSON

Создайте файл `news.json`:

```json
[
  {
    "ticker": "MACRO",
    "source": "BLS Release",
    "content": "US CPI January 2.4% (Lower than expected). Bullish for Tech, Bearish for USD.",
    "sentiment_score": 0.75,
    "ts": "2026-02-13 08:30:00"
  },
  {
    "ticker": "MSFT",
    "source": "Reuters",
    "content": "Microsoft reports strong earnings, cloud revenue up 25%.",
    "sentiment_score": 0.85,
    "ts": "2026-02-13 10:00:00"
  }
]
```

Импорт:
```bash
python news_importer.py import news.json
```

### Просмотр последних новостей

```bash
# Показать последние 10 новостей
python news_importer.py show

# Показать последние 20 новостей
python news_importer.py show 20
```

---

## Рекомендации по обновлению данных

### Цены котировок

1. **Частота обновления**:
   - Ежедневно после закрытия рынка (18:00) - достаточно для дневных данных
   - Для внутридневной торговли потребуется более частое обновление (каждый час или real-time)

2. **Источники данных**:
   - **yfinance** - бесплатно, но с задержкой ~15 минут
   - **Alpaca API** - real-time данные (требует регистрацию)
   - **Interactive Brokers** - профессиональный уровень

3. **Обработка ошибок**:
   - Скрипт автоматически пропускает тикеры с ошибками
   - Логи сохраняются для анализа проблем

### Новости

1. **Источники новостей**:
   - **Ручной ввод** - для важных макро-событий (CPI, FOMC и т.д.)
   - **RSS/API** - можно интегрировать с:
     - Alpha Vantage News API
     - NewsAPI.org
     - Financial Modeling Prep News API
     - Benzinga API

2. **Sentiment анализ**:
   - Можно добавить автоматический расчет sentiment через OpenAI API или другие сервисы
   - Или использовать готовые решения (FinBERT, FinGPT)

3. **Категоризация**:
   - Используйте тикер 'MACRO' для макроэкономических новостей
   - Используйте 'US_MACRO' для новостей США
   - Конкретные тикеры для корпоративных новостей

---

## Примеры интеграции с новостными API

### Alpha Vantage News API

```python
import requests
from news_importer import add_news, load_config
from sqlalchemy import create_engine

API_KEY = "your_alpha_vantage_key"
url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers=MSFT&apikey={API_KEY}"

response = requests.get(url)
data = response.json()

db_url = load_config()
engine = create_engine(db_url)

for article in data.get('feed', []):
    ticker = article.get('ticker_sentiment', [{}])[0].get('ticker', 'MACRO')
    content = article.get('title', '') + ' ' + article.get('summary', '')
    sentiment = float(article.get('overall_sentiment_score', 0.5))
    
    add_news(engine, ticker, article.get('source', 'Alpha Vantage'), 
             content, sentiment)
```

### NewsAPI.org

```python
import requests
from news_importer import add_news, load_config
from sqlalchemy import create_engine

API_KEY = "your_newsapi_key"
url = f"https://newsapi.org/v2/everything?q=Microsoft OR MSFT&apiKey={API_KEY}"

response = requests.get(url)
data = response.json()

db_url = load_config()
engine = create_engine(db_url)

for article in data.get('articles', []):
    add_news(engine, 'MSFT', article.get('source', {}).get('name', 'NewsAPI'),
             article.get('title', '') + ' ' + article.get('description', ''))
```

---

## Мониторинг и логирование

Все скрипты логируют свою работу. Рекомендуется:

1. Настроить ротацию логов
2. Мониторить ошибки обновления
3. Настроить алерты при критических ошибках

Пример настройки логирования в `update_prices.py`:

```python
# Добавить в начало файла
import logging
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler('logs/update_prices.log', maxBytes=10*1024*1024, backupCount=5)
handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)
```

---

## Troubleshooting

### Проблема: yfinance не возвращает данные

**Решение**: 
- Проверьте интернет-соединение
- Убедитесь, что тикер корректен
- Попробуйте другой период загрузки

### Проблема: Дублирование данных

**Решение**: 
- Скрипт использует `ON CONFLICT DO NOTHING`, дубликаты не должны появляться
- Проверьте уникальный индекс на (date, ticker) в таблице quotes

### Проблема: Ошибки подключения к БД

**Решение**:
- Проверьте параметры подключения в `../brats/config.env`
- Убедитесь, что PostgreSQL запущен
- Проверьте права доступа пользователя БД

---

**Последнее обновление**: 2026-02-13


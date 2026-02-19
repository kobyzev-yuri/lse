# План подключения к Must-List источникам новостей

## 🎯 Приоритеты подключения

### Фаза 1: Быстрый старт (1-2 дня) - БЕСПЛАТНО ✅ ВЫПОЛНЕНО
**Приоритет:** Высокий  
**Сложность:** Низкая  
**Стоимость:** $0

1. ✅ **RSS фиды центральных банков** (Fed, BoE, ECB, BoJ) ✅ РЕАЛИЗОВАНО
   - Не требуют API ключей
   - Стабильные источники
   - Автоматический парсинг через `feedparser`
   - **Файл:** `services/rss_news_fetcher.py`

2. ✅ **Economic Calendar через Investing.com** (web scraping) ✅ РЕАЛИЗОВАНО
   - Бесплатно
   - Уже есть опыт парсинга (Гидра для Telegram)
   - Можно использовать существующий код
   - **Файл:** `services/investing_calendar_parser.py`

### Фаза 2: Earnings и новости (2-3 дня) - БЕСПЛАТНЫЙ TIER ✅ ВЫПОЛНЕНО
**Приоритет:** Высокий  
**Сложность:** Средняя  
**Стоимость:** $0 (бесплатные лимиты)

3. ✅ **Alpha Vantage API** (Earnings Calendar + News Sentiment + Economic Indicators + Technical Indicators) ✅ РЕАЛИЗОВАНО
   - Бесплатный tier: 5 запросов/минуту, 500/день
   - Требует регистрацию и API ключ
   - **Файл:** `services/alphavantage_fetcher.py`
   - **Требует:** `ALPHAVANTAGE_KEY` в config.env
   - **Economic Indicators:** CPI, GDP, Federal Funds Rate, Treasury Yield, Unemployment (сохраняются в `knowledge_base` с `event_type='ECONOMIC_INDICATOR'`)
   - **Technical Indicators:** RSI, MACD, Bollinger Bands, ADX, Stochastic (обновляют таблицу `quotes`)

4. ✅ **NewsAPI** (агрегатор новостей) ✅ РЕАЛИЗОВАНО
   - Бесплатный tier: 100 запросов/день
   - Требует регистрацию и API ключ
   - **Файл:** `services/newsapi_fetcher.py`
   - **Требует:** `NEWSAPI_KEY` в config.env

### Фаза 3: Расширенная интеграция (3-5 дней) - ОПЦИОНАЛЬНО
**Приоритет:** Средний  
**Сложность:** Высокая  
**Стоимость:** Зависит от источника

5. ⚠️ **Trading Economics API** (экономический календарь)
   - Платный (от $50/месяц)
   - Альтернатива Investing.com scraping

6. ⚠️ **Bloomberg Terminal API** (институциональные прогнозы)
   - Очень дорого ($2000+/месяц)
   - Альтернатива: парсинг публичных отчетов

---

## 🚀 Начинаем с Фазы 1: RSS фиды центральных банков

### Шаг 1: Создаем модуль для RSS парсинга

**Файл:** `services/rss_news_fetcher.py`

```python
"""
Модуль для получения новостей из RSS фидов центральных банков
"""

import feedparser
import logging
from datetime import datetime
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text

from config_loader import get_database_url

logger = logging.getLogger(__name__)


# RSS фиды центральных банков
RSS_FEEDS = {
    'FOMC_STATEMENT': {
        'url': 'https://www.federalreserve.gov/feeds/press_all.xml',
        'region': 'USA',
        'event_type': 'FOMC_STATEMENT',
        'importance': 'HIGH'
    },
    'FOMC_SPEECH': {
        'url': 'https://www.federalreserve.gov/feeds/speeches.xml',
        'region': 'USA',
        'event_type': 'FOMC_SPEECH',
        'importance': 'HIGH'
    },
    'FOMC_MINUTES': {
        'url': 'https://www.federalreserve.gov/feeds/fomcminutes.xml',
        'region': 'USA',
        'event_type': 'FOMC_MINUTES',
        'importance': 'HIGH'
    },
    'BOE_STATEMENT': {
        'url': 'https://www.bankofengland.co.uk/rss',
        'region': 'UK',
        'event_type': 'BOE_STATEMENT',
        'importance': 'HIGH'
    },
    'ECB_STATEMENT': {
        'url': 'https://www.ecb.europa.eu/rss/press.html',
        'region': 'EU',
        'event_type': 'ECB_STATEMENT',
        'importance': 'HIGH'
    },
    'BOJ_STATEMENT': {
        'url': 'https://www.boj.or.jp/en/announcements/press/index.htm/rss',
        'region': 'Japan',
        'event_type': 'BOJ_STATEMENT',
        'importance': 'HIGH'
    }
}


def parse_rss_feed(feed_config: Dict) -> List[Dict]:
    """
    Парсит RSS фид и возвращает список новостей
    
    Args:
        feed_config: Конфигурация фида (url, region, event_type, importance)
        
    Returns:
        Список словарей с новостями
    """
    url = feed_config['url']
    region = feed_config['region']
    event_type = feed_config['event_type']
    importance = feed_config['importance']
    
    try:
        feed = feedparser.parse(url)
        
        if feed.bozo:
            logger.warning(f"⚠️ Ошибка парсинга RSS фида {url}: {feed.bozo_exception}")
            return []
        
        items = []
        for entry in feed.entries:
            # Парсим дату публикации
            published_time = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published_time = datetime(*entry.published_parsed[:6])
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                published_time = datetime(*entry.updated_parsed[:6])
            else:
                published_time = datetime.now()
            
            item = {
                'title': entry.title,
                'link': entry.link,
                'content': entry.summary if hasattr(entry, 'summary') else entry.title,
                'published': published_time,
                'ticker': 'US_MACRO' if region == 'USA' else 'MACRO',
                'source': f"{region} Central Bank",
                'event_type': event_type,
                'region': region,
                'importance': importance
            }
            items.append(item)
        
        logger.info(f"✅ Получено {len(items)} новостей из {event_type}")
        return items
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении RSS фида {url}: {e}")
        return []


def fetch_all_rss_feeds() -> List[Dict]:
    """
    Получает новости из всех RSS фидов
    
    Returns:
        Список всех новостей
    """
    all_news = []
    
    for feed_name, feed_config in RSS_FEEDS.items():
        logger.info(f"📡 Получение новостей из {feed_name}...")
        news = parse_rss_feed(feed_config)
        all_news.extend(news)
    
    logger.info(f"✅ Всего получено {len(all_news)} новостей из RSS фидов")
    return all_news


def save_news_to_db(news_items: List[Dict], check_duplicates: bool = True):
    """
    Сохраняет новости в базу данных
    
    Args:
        news_items: Список новостей для сохранения
        check_duplicates: Проверять дубликаты по link
    """
    if not news_items:
        logger.info("ℹ️ Нет новостей для сохранения")
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    skipped_count = 0
    
    with engine.begin() as conn:
        for item in news_items:
            try:
                # Проверка дубликатов по link (если включена)
                if check_duplicates:
                    existing = conn.execute(
                        text("""
                            SELECT id FROM knowledge_base 
                            WHERE source = :source 
                            AND link = :link
                        """),
                        {"source": item.get('source', ''), "link": item.get('link', '')}
                    ).fetchone()
                    
                    if existing:
                        skipped_count += 1
                        continue
                
                # Вставляем новость
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base 
                        (ts, ticker, source, content, event_type, region, importance)
                        VALUES (:ts, :ticker, :source, :content, :event_type, :region, :importance)
                    """),
                    {
                        "ts": item['published'],
                        "ticker": item['ticker'],
                        "source": item['source'],
                        "content": f"{item['title']}\n\n{item['content']}\n\nLink: {item['link']}",
                        "event_type": item.get('event_type'),
                        "region": item.get('region'),
                        "importance": item.get('importance')
                    }
                )
                saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении новости '{item.get('title', '')[:50]}...': {e}")
    
    logger.info(f"✅ Сохранено {saved_count} новостей, пропущено дубликатов: {skipped_count}")
    engine.dispose()


def fetch_and_save_rss_news():
    """
    Главная функция: получает новости из RSS и сохраняет в БД
    """
    logger.info("🚀 Начало получения новостей из RSS фидов центральных банков")
    
    # Получаем новости
    news_items = fetch_all_rss_feeds()
    
    # Сохраняем в БД
    if news_items:
        save_news_to_db(news_items)
    
    logger.info("✅ Завершено получение новостей из RSS фидов")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_rss_news()
```

### Шаг 2: Обновляем requirements.txt

```bash
# Добавить в requirements.txt:
feedparser>=6.0.10
```

### Шаг 3: Создаем миграцию БД для новых полей

**Файл:** `scripts/migrate_add_news_fields.py`

```python
"""
Миграция: добавление полей event_type, region, importance в knowledge_base
"""

from sqlalchemy import create_engine, text
from config_loader import get_database_url
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Добавляет новые поля в knowledge_base"""
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    with engine.begin() as conn:
        # Добавляем колонки если их нет
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS event_type VARCHAR(50)
            """))
            logger.info("✅ Добавлена колонка event_type")
        except Exception as e:
            logger.warning(f"⚠️ Колонка event_type: {e}")
        
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS region VARCHAR(20)
            """))
            logger.info("✅ Добавлена колонка region")
        except Exception as e:
            logger.warning(f"⚠️ Колонка region: {e}")
        
        try:
            conn.execute(text("""
                ALTER TABLE knowledge_base 
                ADD COLUMN IF NOT EXISTS importance VARCHAR(10)
            """))
            logger.info("✅ Добавлена колонка importance")
        except Exception as e:
            logger.warning(f"⚠️ Колонка importance: {e}")
        
        # Создаем индексы
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_kb_event_type 
                ON knowledge_base(event_type)
            """))
            logger.info("✅ Создан индекс idx_kb_event_type")
        except Exception as e:
            logger.warning(f"⚠️ Индекс event_type: {e}")
        
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_kb_region 
                ON knowledge_base(region)
            """))
            logger.info("✅ Создан индекс idx_kb_region")
        except Exception as e:
            logger.warning(f"⚠️ Индекс region: {e}")
    
    logger.info("✅ Миграция завершена")
    engine.dispose()


if __name__ == "__main__":
    migrate()
```

### Шаг 4: Тестируем подключение

```bash
# 1. Установить зависимости
pip install feedparser>=6.0.10

# 2. Запустить миграцию
python scripts/migrate_add_news_fields.py

# 3. Протестировать RSS парсер
python services/rss_news_fetcher.py
```

---

## 📅 Следующие шаги (Фаза 2)

### Alpha Vantage API - Earnings Calendar

**Регистрация:**
1. Перейти на https://www.alphavantage.co/support/#api-key
2. Заполнить форму (бесплатно)
3. Получить API ключ

**Использование:**
```python
# services/alphavantage_fetcher.py
import requests
import csv
from io import StringIO

def fetch_earnings_calendar(api_key: str, symbol: str = None):
    """
    Получает календарь earnings через Alpha Vantage
    
    Args:
        api_key: API ключ Alpha Vantage
        symbol: Тикер (опционально, если None - все)
    """
    url = "https://www.alphavantage.co/query"
    params = {
        'function': 'EARNINGS_CALENDAR',
        'apikey': api_key
    }
    if symbol:
        params['symbol'] = symbol
    
    response = requests.get(url, params=params)
    
    # Alpha Vantage возвращает CSV
    csv_data = response.text
    reader = csv.DictReader(StringIO(csv_data))
    
    earnings = []
    for row in reader:
        earnings.append({
            'symbol': row.get('symbol'),
            'reportDate': row.get('reportDate'),
            'estimate': row.get('estimate'),
            'currency': row.get('currency')
        })
    
    return earnings
```

### NewsAPI - Агрегатор новостей

**Регистрация:**
1. Перейти на https://newsapi.org/register
2. Заполнить форму (бесплатный tier: 100 запросов/день)
3. Получить API ключ

**Использование:**
```python
# services/newsapi_fetcher.py
import requests
from datetime import datetime, timedelta

def fetch_newsapi_articles(api_key: str, query: str, sources: str = 'reuters,bloomberg'):
    """
    Получает новости через NewsAPI
    
    Args:
        api_key: API ключ NewsAPI
        query: Поисковый запрос (например, "Federal Reserve")
        sources: Источники через запятую
    """
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': query,
        'sources': sources,
        'language': 'en',
        'sortBy': 'publishedAt',
        'apiKey': api_key,
        'from': (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    }
    
    response = requests.get(url, params=params)
    data = response.json()
    
    articles = []
    for article in data.get('articles', []):
        articles.append({
            'title': article['title'],
            'content': article.get('description', '') + '\n\n' + article.get('content', ''),
            'source': article['source']['name'],
            'published': datetime.fromisoformat(article['publishedAt'].replace('Z', '+00:00')),
            'url': article['url']
        })
    
    return articles
```

---

## 🔄 План настройки Cron для новостей

### Вариант 1: Через общий скрипт (рекомендуется)

Задача новостей уже добавлена в `setup_cron.sh`. Запустите один раз:

```bash
cd /home/cnn/lse
./setup_cron.sh
```

Будет установлено:
- **Обновление цен:** ежедневно в 22:00
- **Торговый цикл:** 9:00, 13:00, 17:00 (пн–пт)
- **Новости:** каждый час в :00 (RSS, NewsAPI, Alpha Vantage)

Лог новостей: `logs/news_fetch.log`

### Вариант 2: Добавить только новости вручную

```bash
crontab -e
```

Добавьте строку (подставьте свой путь к проекту и python):

```bash
# Новости LSE — каждый час
0 * * * * cd /home/cnn/lse && /usr/bin/python3 scripts/fetch_news_cron.py >> /home/cnn/lse/logs/news_fetch.log 2>&1
```

Если используете conda env **py11** (для feedparser), укажите полный путь к python этого окружения:

```bash
0 * * * * cd /home/cnn/lse && /path/to/anaconda3/envs/py11/bin/python scripts/fetch_news_cron.py >> /home/cnn/lse/logs/news_fetch.log 2>&1
```

Узнать путь: `conda activate py11 && which python`

### Проверка

```bash
# Список задач
crontab -l

# Ручной запуск (тест)
cd /home/cnn/lse && python3 scripts/fetch_news_cron.py

# Просмотр лога
tail -f logs/news_fetch.log
```

### Скрипт

**Файл:** `scripts/fetch_news_cron.py` — по очереди вызывает RSS, Investing.com, Alpha Vantage, NewsAPI и пишет результат в БД и в лог.

---

## ✅ Чеклист реализации

### Фаза 1 (RSS фиды) - 1-2 дня ✅ ВЫПОЛНЕНО
- [x] Создать `services/rss_news_fetcher.py` ✅
- [x] Добавить `feedparser` в `requirements.txt` ✅
- [x] Создать миграцию `scripts/migrate_add_news_fields.py` ✅
- [ ] Запустить миграцию (требуется выполнение)
- [ ] Протестировать парсинг RSS фидов (требуется тестирование)
- [ ] Сохранить тестовые новости в БД (требуется тестирование)
- [x] Добавить cron задачу для автоматического обновления ✅ (через ./setup_cron.sh)

### Фаза 2 (API источники) - 2-3 дня ✅ ВЫПОЛНЕНО
- [ ] Зарегистрироваться в Alpha Vantage, получить API ключ (требуется регистрация)
- [x] Создать `services/alphavantage_fetcher.py` ✅
- [ ] Протестировать получение Earnings Calendar (требуется тестирование с API ключом)
- [ ] Зарегистрироваться в NewsAPI, получить API ключ (требуется регистрация)
- [x] Создать `services/newsapi_fetcher.py` ✅
- [ ] Протестировать получение новостей (требуется тестирование с API ключом)
- [x] Интегрировать в `scripts/fetch_news_cron.py` ✅

### Дополнительно ✅ ВЫПОЛНЕНО
- [x] Создать `services/investing_calendar_parser.py` ✅
- [x] Создать `scripts/fetch_news_cron.py` для автоматизации ✅

### Фаза 3 (Investing.com scraping) - 2-3 дня
- [ ] Изучить структуру Investing.com Economic Calendar
- [ ] Создать `services/investing_calendar_parser.py`
- [ ] Протестировать парсинг для всех регионов
- [ ] Добавить обработку ошибок и retry логику
- [ ] Интегрировать в cron

---

## 📊 Мониторинг

**Логи:**
- `logs/news_fetch.log` - общий лог получения новостей
- `logs/news_cron.log` - лог cron задач

**Метрики:**
- Количество новостей в день по источникам
- Количество дубликатов (показывает эффективность фильтрации)
- Ошибки парсинга/API запросов

---

## 📝 Итоговый статус реализации

### ✅ Реализовано (2026-02-19)

1. **RSS фиды центральных банков** ✅
   - Файл: `services/rss_news_fetcher.py`
   - Поддерживает: Fed (FOMC), BoE, ECB, BoJ
   - Не требует API ключей

2. **Investing.com Economic Calendar** ✅
   - Файл: `services/investing_calendar_parser.py`
   - Поддерживает: USA, UK, EU, Japan, China, Switzerland
   - Web scraping через BeautifulSoup

3. **Alpha Vantage API** ✅
   - Файл: `services/alphavantage_fetcher.py`
   - Поддерживает: 
     - Earnings Calendar (сохраняется в `knowledge_base`)
     - News Sentiment (сохраняется в `knowledge_base` с sentiment_score)
     - Economic Indicators: CPI, GDP, Federal Funds Rate, Treasury Yield, Unemployment (сохраняются в `knowledge_base` с `event_type='ECONOMIC_INDICATOR'`)
     - Technical Indicators: RSI, MACD, Bollinger Bands, ADX, Stochastic (обновляют таблицу `quotes`)
   - Требует: `ALPHAVANTAGE_KEY` в config.env
   - Настройки: `ALPHAVANTAGE_FETCH_ECONOMIC=true`, `ALPHAVANTAGE_FETCH_TECHNICAL=true` (в config.env)

4. **NewsAPI** ✅
   - Файл: `services/newsapi_fetcher.py`
   - Поддерживает: макро-новости, агрегация из Reuters/Bloomberg
   - Требует: `NEWSAPI_KEY` в config.env

5. **Автоматизация** ✅
   - Файл: `scripts/fetch_news_cron.py`
   - Объединяет все источники в один скрипт

6. **Миграция БД** ✅
   - Файл: `scripts/migrate_add_news_fields.py`
   - Добавляет: event_type, region, importance, link

### 🔧 Следующие шаги

1. **Запустить миграцию БД:**
   ```bash
   python scripts/migrate_add_news_fields.py
   ```

2. **Установить зависимости:**
   ```bash
   pip install feedparser>=6.0.10 lxml>=4.9.0
   ```

3. **Получить API ключи (опционально):**
   - Alpha Vantage: https://www.alphavantage.co/support/#api-key
   - NewsAPI: https://newsapi.org/register
   - Добавить в `config.env`:
     ```env
     ALPHAVANTAGE_KEY=your_key_here
     NEWSAPI_KEY=your_key_here
     ```

4. **Протестировать парсинг:**
   ```bash
   # RSS фиды (работает без API ключей)
   python services/rss_news_fetcher.py
   
   # Investing.com календарь
   python services/investing_calendar_parser.py
   
   # Alpha Vantage (требует API ключ)
   python services/alphavantage_fetcher.py
   
   # NewsAPI (требует API ключ)
   python services/newsapi_fetcher.py
   
   # Все источники сразу
   python scripts/fetch_news_cron.py
   ```

5. **Настроить cron:**
   ```bash
   ./setup_cron.sh
   ```
   Задача новостей (каждый час) входит в скрипт. См. раздел «План настройки Cron для новостей» выше.

### 📊 Что осталось сделать

- [x] Настроить API ключ для NewsAPI ✅ (добавлен в config.env)
- [x] Протестировать модули (RSS, NewsAPI, Alpha Vantage работают) ✅
- [x] Настроить API ключ для Alpha Vantage ✅
- [ ] Добавить обработку ошибок и retry логику
- [x] Настроить cron: запустить `./setup_cron.sh` — задача новостей добавлена (каждый час)
- [ ] Интегрировать с LLM для анализа impact (см. NEWS_INTEGRATION_PLAN.md)

### 🧪 Статус тестирования

См. [NEWS_TESTING_STATUS.md](NEWS_TESTING_STATUS.md) для детального плана тестирования каждого скрипта.

### 🧪 Как протестировать изменения (после правок)

Из корня проекта (`~/lse`), с активированным окружением (например `conda activate py11`):

1. **Всё сразу (как в cron):**
   ```bash
   python scripts/fetch_news_cron.py
   ```
   Логи пишутся в консоль и в `logs/news_fetch.log`.

2. **Только Alpha Vantage** (earnings + новости + экономические + технические индикаторы):
   ```bash
   python services/alphavantage_fetcher.py
   ```
   Используются тикеры `MSFT`, `SNDK`, `MU`. Проверьте в логах: количество сохранённых earnings, полученные экономические индикаторы, обновление технических в `quotes`.

3. **Только Investing.com календарь:**
   ```bash
   python services/investing_calendar_parser.py
   ```
   Если таблица не найдена, можно сохранить HTML для разбора:
   ```bash
   INVESTING_CALENDAR_DEBUG_HTML=1 python services/investing_calendar_parser.py
   ```
   Файлы появятся в `/tmp/investing_calendar_USA.html` и т.д.

4. **По одному источнику через общий скрипт:**
   ```bash
   ./test_all_news_sources.sh
   ```
   По очереди запускает RSS, Investing.com, NewsAPI, Alpha Vantage.

5. **Проверка БД после теста:**
   ```bash
   # Последние записи в knowledge_base (новости, earnings, экономические индикаторы)
   psql $DATABASE_URL -c "SELECT ts, ticker, source, event_type, LEFT(content, 60) FROM knowledge_base ORDER BY ts DESC LIMIT 15;"
   
   # Технические индикаторы в quotes (RSI, MACD и т.д.)
   psql $DATABASE_URL -c "SELECT date, ticker, rsi, macd, adx FROM quotes WHERE rsi IS NOT NULL ORDER BY date DESC LIMIT 10;"
   ```
   (Подставьте свою `DATABASE_URL` или используйте переменную из `config.env`.)

---

**Статус:** Реализовано и протестировано; cron настраивается через `./setup_cron.sh`  
**Последнее обновление:** 2026-02-19

# План подключения к Must-List источникам новостей

## Что реализовано, что нет, как получить все источники для теста

| Источник | Реализовано | Работает без доп. условий | Причина ограничений |
|----------|-------------|---------------------------|---------------------|
| **RSS центральных банков** (Fed, BoE, ECB, BoJ) | Да | Да | — |
| **NewsAPI** | Да | Да (нужен ключ) | Бесплатно: 100 запросов/день |
| **Alpha Vantage** (Earnings + News Sentiment) | Да | Да (нужен ключ) | Бесплатно: ~25 запросов/день, 1 запрос/сек |
| **Alpha Vantage** (Economic Indicators) | Код есть | Нет | В cron выключено (`ALPHAVANTAGE_FETCH_ECONOMIC=false`); на бесплатном плане API часто возвращает премиум/пустой ответ |
| **Alpha Vantage** (Technical Indicators) | Код есть | Частично | В cron выключено; часть эндпоинтов (MACD и др.) — премиум |
| **Investing.com Economic Calendar** | Код есть | Нет | Таблица на сайте подгружается через JavaScript; `requests` + BeautifulSoup получают HTML без таблицы → 0 событий |

**Как добиться получения всех значимых источников для тестирования:**

1. **Обязательно (работает бесплатно или с бесплатными ключами):**
   - Выполнить миграцию БД: `python scripts/migrate_add_news_fields.py`
   - В `config.env` добавить: `ALPHAVANTAGE_KEY`, `NEWSAPI_KEY`
   - Запустить все источники разом: `python scripts/fetch_news_cron.py`  
   В БД попадут: RSS (Fed, BoE, ECB, BoJ), NewsAPI, Alpha Vantage (Earnings + News Sentiment). Investing.com в этом прогоне даст 0 событий; Economic/Technical от AV не запрашиваются по умолчанию.

2. **Если нужны макро-события (экономический календарь):**
   - **Вариант A:** включить Alpha Vantage Economic: в `config.env` задать `ALPHAVANTAGE_FETCH_ECONOMIC=true`. На бесплатном плане данные часто не приходят; стабильно — при платной подписке AV.
   - **Вариант B:** парсер Investing.com без доп. затрат не даёт данных (JS). Чтобы получать данные с Investing.com — нужен headless browser (Selenium/Playwright) или платный API (например Trading Economics).

3. **Проверка после прогона:**  
   `psql $DATABASE_URL -c "SELECT ts, ticker, source, event_type FROM knowledge_base ORDER BY ts DESC LIMIT 20;"`

---

### Частые вопросы: MANUAL, NewsAPI в списке, чего не хватает

**1. Откуда берётся источник MANUAL? Я не загружал своих новостей.**

В поле `source` значение **MANUAL** появляется в двух случаях:

- **Миграция из старой таблицы `trade_kb`.** Скрипт `scripts/migrate_trade_kb_to_knowledge_base.py` переносит все строки из `trade_kb` в `knowledge_base` и проставляет им `source = 'MANUAL'`. Если этот скрипт запускали, то тысячи записей с MANUAL — это как раз перенесённые старые данные (сделки, заметки, события из trade_kb), а не загруженные вручную новости.
- **Добавление событий через код без указания источника.** В `services/vector_kb.py` при вызове `add_event()` без параметра `source` по умолчанию подставляется `'MANUAL'`.

Итого: если вы не вызывали вручную добавление новостей, то MANUAL в вашей выборке почти наверняка из миграции `trade_kb` → `knowledge_base`.

**2. Почему в списке источников нет NewsAPI?**

NewsAPI **используется** в cron (`fetch_news_cron.py`), но в БД в поле `source` сохраняется **название издания** из ответа API (Reuters, Bloomberg, The Globe and Mail, Yahoo Finance, Business Wire и т.д.), а не строка «NewsAPI». Код: `services/newsapi_fetcher.py` — при сохранении берётся `item.get('source', 'NewsAPI')`, где `item['source']` приходит от NewsAPI как имя источника. Поэтому в запросе по `source` вы видите Bloomberg, The Globe and Mail и др. — это и есть новости, полученные через NewsAPI.

**3. Каких существенных источников не хватает для работы?**

Кратко по «достаточности»:

| Нужно для работы | Есть у нас | Комментарий |
|------------------|------------|-------------|
| Новости ЦБ (Fed, ECB, BoE, BoJ) | ✅ RSS | EU Central Bank, USA Central Bank, UK Central Bank, Japan Central Bank в выборке — это они. |
| Макро-новости (аггрегаторы) | ✅ NewsAPI | У вас в списке как Bloomberg, The Globe and Mail, Yahoo Finance, Business Wire, Mint, BNY, The Motley Fool, FinancialContent. |
| Календарь отчётов (earnings) | ✅ Alpha Vantage | «Alpha Vantage Earnings Calendar» в выборке. |
| Новости + sentiment по тикерам | ✅ Alpha Vantage | При наличии ключа; лимит ~25 запросов/день. |
| Экономический календарь (даты CPI, NFP, ставки и т.д.) | ❌ Нет стабильно | Investing.com не отдаёт данные (JS); Alpha Vantage Economic в cron выключен, на free tier часто пусто. |
| Числовые макро-ряды (CPI, GDP, ставки) по регионам | ⚠️ Частично | Только при включённом Alpha Vantage Economic и плане, где API отдаёт данные; в основном США. |

**Для базовой работы** (новости ЦБ, макро-новости, earnings, опционально sentiment) **достаточно** того, что уже есть: RSS + NewsAPI + Alpha Vantage (Earnings + News Sentiment). Не хватает в первую очередь **стабильного экономического календаря** (даты релизов) и **числовых макро-индикаторов** по разным регионам — это либо платный Alpha Vantage / Trading Economics, либо доработка парсера Investing.com (например, через headless browser).

Единый скрипт запуска: `scripts/fetch_news_cron.py`. Отдельные модули: `services/rss_news_fetcher.py`, `services/newsapi_fetcher.py`, `services/alphavantage_fetcher.py`, `services/investing_calendar_parser.py`.

### Какие важные финансовые индикаторы отсутствуют в knowledge_base

**Сейчас в knowledge_base попадают (когда работают источники):**
- **Экономические индикаторы (Alpha Vantage, при включённом Economic):** CPI, REAL_GDP, FEDERAL_FUNDS_RATE, TREASURY_YIELD (10Y), UNEMPLOYMENT — только США; на бесплатном плане AV часто не отдаются.
- **События/новости:** решения ЦБ (FOMC, BoE, ECB, BoJ) через RSS; макро-новости через NewsAPI; earnings через AV. Числовых рядов по еврозоне/UK/Японии нет.

**Важные индикаторы, которых нет в knowledge_base:**

| Индикатор | Зачем нужен | Где взять у нас |
|-----------|-------------|------------------|
| **PPI** (Producer Price Index) | Инфляция на уровне производителей | Не загружаем. AV поддерживает; парсер Investing.com умеет тип, но данных нет (JS). |
| **Retail Sales** | Потребление, рецессии | AV есть эндпоинт, в `fetch_economic_indicators()` не добавлен. |
| **Nonfarm Payrolls (NFP)** | Занятость, ключевой релиз США | У AV есть NONFARM_PAYROLL; у нас только UNEMPLOYMENT. |
| **Durable Goods Orders** | Заказы, цикл | AV есть, не запрашиваем. |
| **Consumer Sentiment** (Michigan) | Ожидания домохозяйств | AV есть, не запрашиваем. |
| **Inflation Expectation** | Инфляционные ожидания | AV есть, не запрашиваем. |
| **PCE / PCE Price Index** | Предпочитаемый ФРС показатель инфляции | В AV не проверялся; часто только через Fed или платные API. |
| **PMI** (ISM Manufacturing/Services) | Цикл, рецессии | У нас нет. AV — нет в нашем коде; Investing.com умеет тип, данных нет. |
| **Treasury 2Y** | Кривая доходности (2Y–10Y) | Загружаем только 10Y; 2Y в AV можно задать через параметр maturity. |
| **Housing Starts / Building Permits** | Жилищный цикл | Не загружаем; при необходимости — отдельный источник. |
| **Регионы кроме США** | CPI/GDP/ставки EU, UK, JP | Только новости ЦБ (RSS). Числовых рядов по еврозоне/UK/Японии в knowledge_base нет. |

**Как добавить часть недостающих (через Alpha Vantage):**  
В `services/alphavantage_fetcher.py` в `fetch_economic_indicators()` уже вызываются CPI, REAL_GDP, FEDERAL_FUNDS_RATE, TREASURY_YIELD, UNEMPLOYMENT. Туда же можно добавить вызовы (с учётом лимита 1 запрос/сек и расхода лимита): `RETAIL_SALES`, `NONFARM_PAYROLL`, `INFLATION`, `DURABLE_GOODS_ORDERS`, `CONSUMER_SENTIMENT`, `INFLATION_EXPECTATION`. Для 2Y: в `TREASURY_YIELD` передать `maturity='2year'` (и при необходимости сохранять с другим source/меткой). Учитывать, что на бесплатном плане многие из этих эндпоинтов могут быть премиум или лимитированы.

---

## 🎯 Приоритеты подключения (фазы)

### Фаза 1: Быстрый старт — БЕСПЛАТНО ✅
1. **RSS фиды центральных банков** — реализовано, работает. Файл: `services/rss_news_fetcher.py`
2. **Investing.com Economic Calendar** — код есть (`services/investing_calendar_parser.py`), данные не приходят: таблица подгружается через JS.

### Фаза 2: Earnings и новости — бесплатные лимиты ✅
3. **Alpha Vantage** — Earnings + News Sentiment работают при наличии ключа; Economic/Technical в cron выключены, на бесплатном плане Economic часто недоступен. Файл: `services/alphavantage_fetcher.py`
4. **NewsAPI** — реализовано, работает при наличии ключа. Файл: `services/newsapi_fetcher.py`

### Фаза 3: Опционально
5. Trading Economics API (платный), 6. Bloomberg (очень дорого) — не реализованы.

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

## 📊 Мониторинг

**Логи:**
- `logs/news_fetch.log` - общий лог получения новостей
- `logs/news_cron.log` - лог cron задач

**Метрики:**
- Количество новостей в день по источникам
- Количество дубликатов (показывает эффективность фильтрации)
- Ошибки парсинга/API запросов

---

## Шаги для теста и продакшена

Итоговый статус по каждому источнику — в таблице в начале документа («Что реализовано, что нет…»).

1. Миграция БД: `python scripts/migrate_add_news_fields.py`
2. Зависимости: `pip install feedparser>=6.0.10 lxml>=4.9.0`
3. В `config.env`: `ALPHAVANTAGE_KEY`, `NEWSAPI_KEY` (при необходимости — `ALPHAVANTAGE_FETCH_ECONOMIC=true` для макро).
4. Все источники разом: `python scripts/fetch_news_cron.py` (логи также в `logs/news_fetch.log`).
5. Cron: `./setup_cron.sh` (новости — каждый час).

Проверка БД:  
`psql $DATABASE_URL -c "SELECT ts, ticker, source, event_type FROM knowledge_base ORDER BY ts DESC LIMIT 20;"`

Детальный план тестов по скриптам: [NEWS_TESTING_STATUS.md](NEWS_TESTING_STATUS.md).

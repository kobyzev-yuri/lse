# План интеграции новостей для торговых рекомендаций

## 📋 Обзор

Этот документ описывает план интеграции новостных источников в LSE Trading System для улучшения качества торговых решений. План включает:
- **Must-list источников** (обязательные категории новостей)
- **Структуру хранения** (расширение БД)
- **Метод использования** (LLM анализ с прогнозом impact по активам)
- **Популярные источники** и способы подключения

---

## 🎯 Must-List: Обязательные категории новостей

### 1. Economic Calendar (Экономический календарь)

**Регионы:**
- 🇺🇸 **USA** (BLS, Fed, Treasury)
- 🇬🇧 **UK** (ONS, BoE)
- 🇪🇺 **EU** (Eurostat, ECB)
- 🇯🇵 **Japan** (BoJ, Statistics Bureau)
- 🇨🇳 **China** (NBS, PBOC)
- 🇨🇭 **Switzerland** (SNB, SECO)

**Типы событий:**
- `RATE_DECISION` - решения по процентным ставкам
- `CPI` - Consumer Price Index (инфляция)
- `PPI` - Producer Price Index
- `NFP` - Non-Farm Payrolls (только USA)
- `PMI` - Purchasing Managers Index
- `GDP` - Gross Domestic Product
- `UNEMPLOYMENT` - уровень безработицы
- `RETAIL_SALES` - розничные продажи
- `INDUSTRIAL_PRODUCTION` - промышленное производство
- `TRADE_BALANCE` - торговый баланс

**Важность событий:**
- `HIGH` - критически важные (RATE_DECISION, NFP, CPI)
- `MEDIUM` - важные (GDP, PMI, UNEMPLOYMENT)
- `LOW` - информационные (TRADE_BALANCE, другие)

### 2. Earnings (Отчеты о прибыли)

**Обязательные тикеры:**
- Все тикеры из торгового списка (MSFT, SNDK, MU, LITE, ALAB, TER и т.д.)

**Майки (для сентимента рынка):**
- **FAANG**: AAPL, AMZN, GOOGL, META, NFLX
- **Mega-cap IT**: MSFT, NVDA, TSLA
- **Банки**: JPM, BAC, WFC, GS, MS
- **Индексные**: SPY, QQQ компоненты

**Структура данных:**
- `expected` - ожидаемая прибыль (EPS)
- `actual` - фактическая прибыль
- `surprise` - сюрприз (actual - expected)
- `revenue` - выручка
- `guidance` - прогноз компании

### 3. FOMC / Central Bank Communications

**Типы событий:**
- `FOMC_STATEMENT` - заявление после заседания
- `FOMC_MINUTES` - протоколы заседаний (публикуются позже)
- `FOMC_SPEECH` - выступления членов FOMC
- `BOE_STATEMENT` - заявления BoE
- `ECB_STATEMENT` - заявления ECB
- `BOJ_STATEMENT` - заявления BoJ

**Особенности:**
- Minutes публикуются через 3 недели после решения
- Речи могут быть hawkish/dovish
- Влияют на доллар, золото, риск-активы

### 4. Geopolitical Risk Events

**Типы:**
- `GEOPOLITICAL_CONFLICT` - военные конфликты
- `SANCTIONS` - санкции
- `TRADE_WAR` - торговые войны
- `ELECTION` - выборы (особенно USA)

**Примеры:**
- Угроза удара USA по Ирану → нефть вверх, доллар вверх (safe haven)
- Комментарии FRS о повышении ставки → доллар вверх, золото нейтрально

### 5. Institutional Forecasts (Прогнозы крупных домов)

**Источники:**
- Goldman Sachs, JPMorgan, Morgan Stanley, Bank of America, Citigroup
- Bloomberg Intelligence, Reuters Polls

**Типы прогнозов:**
- `MARKET_FORECAST` - прогноз движения рынка
- `SECTOR_FORECAST` - прогноз по секторам
- `ALGORITHMIC_FLOW` - прогнозы по алгофондам (например, "алгофонды будут разгружаться")
- `RISK_ASSESSMENT` - оценка рисков

**Примеры:**
- "Goldman прогнозирует sell-off в mega-cap IT" → MSFT/AMZN вниз, SNDK/Nebius вверх
- Нужно отслеживать исполнение прогнозов для обучения модели

---

## 🗄️ Структура хранения

### Расширение таблицы `knowledge_base`

**Текущая структура:**
```sql
CREATE TABLE knowledge_base (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP,
    ticker VARCHAR(10),
    source VARCHAR(100),
    content TEXT,
    sentiment_score DECIMAL(3,2),
    insight TEXT
);
```

**Предлагаемое расширение:**
```sql
-- Добавляем новые колонки
ALTER TABLE knowledge_base 
    ADD COLUMN IF NOT EXISTS event_type VARCHAR(50),  -- RATE_DECISION, EARNINGS, FOMC_MINUTES и т.д.
    ADD COLUMN IF NOT EXISTS region VARCHAR(20),        -- USA, UK, EU, Japan, China, Switzerland
    ADD COLUMN IF NOT EXISTS importance VARCHAR(10),   -- HIGH, MEDIUM, LOW
    ADD COLUMN IF NOT EXISTS is_forecast BOOLEAN DEFAULT FALSE,  -- True для прогнозов
    ADD COLUMN IF NOT EXISTS forecast_date TIMESTAMP,  -- Дата, когда прогноз должен сбыться
    ADD COLUMN IF NOT EXISTS impact_json JSONB;         -- Структурированный impact от LLM

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_kb_event_type ON knowledge_base(event_type);
CREATE INDEX IF NOT EXISTS idx_kb_region ON knowledge_base(region);
CREATE INDEX IF NOT EXISTS idx_kb_importance ON knowledge_base(importance);
CREATE INDEX IF NOT EXISTS idx_kb_is_forecast ON knowledge_base(is_forecast);
CREATE INDEX IF NOT EXISTS idx_kb_ts_ticker ON knowledge_base(ts, ticker);
```

**Структура `impact_json`:**
```json
{
  "market_impact": {
    "SPY": {"direction": "UP", "confidence": 0.7, "horizon": "1-3d"},
    "QQQ": {"direction": "DOWN", "confidence": 0.8, "horizon": "1-2d"}
  },
  "fx_impact": {
    "DXY": {"direction": "UP", "confidence": 0.9, "horizon": "1-7d"},
    "EURUSD=X": {"direction": "DOWN", "confidence": 0.8, "horizon": "1-7d"}
  },
  "commodity_impact": {
    "CL=F": {"direction": "UP", "confidence": 0.85, "horizon": "1d"},
    "XAUUSD=X": {"direction": "NEUTRAL", "confidence": 0.6, "horizon": "1d"}
  },
  "sector_impact": {
    "IT": {"direction": "DOWN", "confidence": 0.75},
    "ENERGY": {"direction": "UP", "confidence": 0.8}
  },
  "ticker_impact": {
    "MSFT": {"direction": "DOWN", "confidence": 0.7, "reason": "алгофонды разгружаются"},
    "SNDK": {"direction": "UP", "confidence": 0.65, "reason": "бенефициар от перетока капитала"}
  },
  "risk_regime": "RISK_OFF",  // RISK_ON, RISK_OFF, NEUTRAL
  "safe_haven": "USD",  // USD, XAUUSD=X, BONDS
  "reasoning": "Комментарии FRS вернули доллару статус safe haven, инвесторы бегут в доллар вместо золота"
}
```

### Таблица для отслеживания исполнения прогнозов

```sql
CREATE TABLE IF NOT EXISTS forecast_tracking (
    id SERIAL PRIMARY KEY,
    forecast_news_id INTEGER REFERENCES knowledge_base(id),
    forecast_date TIMESTAMP,
    actual_date TIMESTAMP,
    ticker VARCHAR(20),
    expected_direction VARCHAR(10),  -- UP, DOWN, NEUTRAL
    actual_direction VARCHAR(10),
    expected_magnitude DECIMAL(5,2),  -- ожидаемое изменение в %
    actual_magnitude DECIMAL(5,2),
    forecast_accuracy DECIMAL(3,2),  -- 0.0-1.0 (насколько точно сбылся)
    notes TEXT
);
```

---

## 🔌 Популярные источники и подключение

### 1. Economic Calendar

#### Investing.com Economic Calendar
**URL:** `https://www.investing.com/economic-calendar/`

**Метод подключения:**
- **Web Scraping** (как в Гидре для Telegram)
- Используйте библиотеку `beautifulsoup4` или `selenium`
- Парсинг HTML таблицы с событиями

**Пример кода:**
```python
import requests
from bs4 import BeautifulSoup
from datetime import datetime

def fetch_investing_calendar(region='USA', days_ahead=7):
    """
    Парсит экономический календарь Investing.com
    
    Args:
        region: USA, UK, EU, Japan, China, Switzerland
        days_ahead: количество дней вперед
    """
    url = f"https://www.investing.com/economic-calendar/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    # Параметры фильтрации по региону
    params = {
        'country': region,
        'timeZone': '8',  # UTC
        'timeFilter': 'timeRemain',
        'currentTab': 'today',
        'limit_from': 0
    }
    
    response = requests.get(url, headers=headers, params=params)
    soup = BeautifulSoup(response.content, 'html.parser')
    
    events = []
    # Парсинг таблицы событий (структура может меняться)
    table = soup.find('table', {'id': 'economicCalendarData'})
    if table:
        rows = table.find_all('tr')[1:]  # Пропускаем заголовок
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 5:
                event = {
                    'time': cols[0].text.strip(),
                    'currency': cols[1].text.strip(),
                    'importance': cols[2].get('title', ''),  # HIGH, MEDIUM, LOW
                    'event': cols[3].text.strip(),
                    'actual': cols[4].text.strip() if len(cols) > 4 else None,
                    'forecast': cols[5].text.strip() if len(cols) > 5 else None,
                    'previous': cols[6].text.strip() if len(cols) > 6 else None,
                    'region': region
                }
                events.append(event)
    
    return events
```

**Альтернативы:**
- **Trading Economics API** (платный, но структурированный)
- **FXStreet Economic Calendar** (бесплатный RSS)
- **ForexFactory Calendar** (парсинг)

#### Trading Economics API
**URL:** `https://tradingeconomics.com/api`

**Подключение:**
```python
import requests

def fetch_trading_economics_calendar(api_key, countries=['united-states', 'united-kingdom']):
    """
    Получает экономический календарь через Trading Economics API
    
    Требуется регистрация и API ключ (есть бесплатный tier)
    """
    url = "https://api.tradingeconomics.com/calendar"
    params = {
        'c': ','.join(countries),
        'f': 'json',
        'key': api_key
    }
    
    response = requests.get(url, params=params)
    return response.json()
```

### 2. Earnings Calendar

#### Alpha Vantage API
**URL:** `https://www.alphavantage.co/documentation/`

**Подключение:**
```python
import requests

def fetch_earnings_calendar(api_key, symbol=None):
    """
    Получает календарь earnings через Alpha Vantage
    
    Args:
        api_key: API ключ (бесплатный, но с лимитами)
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
    # Возвращает CSV, нужно парсить
    return response.text
```

#### Yahoo Finance (yfinance)
**Метод:** Парсинг через `yfinance`

```python
import yfinance as yf

def get_earnings_date(ticker):
    """
    Получает дату следующего earnings через yfinance
    """
    stock = yf.Ticker(ticker)
    info = stock.info
    earnings_date = info.get('nextEarningsDate')
    return earnings_date
```

#### EarningsWhispers
**URL:** `https://www.earningswhispers.com/`

**Метод:** Web scraping (бесплатный календарь)

### 3. FOMC / Central Bank Communications

#### Federal Reserve RSS Feeds
**URLs:**
- FOMC Statements: `https://www.federalreserve.gov/feeds/press_all.xml`
- Speeches: `https://www.federalreserve.gov/feeds/speeches.xml`
- Minutes: `https://www.federalreserve.gov/feeds/fomcminutes.xml`

**Подключение:**
```python
import feedparser
from datetime import datetime

def fetch_fed_rss(feed_type='statements'):
    """
    Парсит RSS фиды Федерального Резерва
    
    Args:
        feed_type: 'statements', 'speeches', 'minutes'
    """
    feeds = {
        'statements': 'https://www.federalreserve.gov/feeds/press_all.xml',
        'speeches': 'https://www.federalreserve.gov/feeds/speeches.xml',
        'minutes': 'https://www.federalreserve.gov/feeds/fomcminutes.xml'
    }
    
    feed = feedparser.parse(feeds[feed_type])
    
    items = []
    for entry in feed.entries:
        items.append({
            'title': entry.title,
            'link': entry.link,
            'published': entry.published,
            'summary': entry.summary,
            'event_type': 'FOMC_STATEMENT' if feed_type == 'statements' else f'FOMC_{feed_type.upper()}'
        })
    
    return items
```

#### Bank of England
**RSS:** `https://www.bankofengland.co.uk/rss`

#### European Central Bank
**RSS:** `https://www.ecb.europa.eu/rss/press.html`

### 4. News Aggregators

#### NewsAPI
**URL:** `https://newsapi.org/`

**Подключение:**
```python
import requests

def fetch_newsapi_articles(api_key, query, sources='reuters,bloomberg', language='en'):
    """
    Получает новости через NewsAPI
    
    Args:
        api_key: API ключ (бесплатный tier: 100 запросов/день)
        query: Поисковый запрос (например, "Federal Reserve rate")
        sources: Источники (reuters,bloomberg,financial-times)
        language: Язык (en)
    """
    url = "https://newsapi.org/v2/everything"
    params = {
        'q': query,
        'sources': sources,
        'language': language,
        'sortBy': 'publishedAt',
        'apiKey': api_key
    }
    
    response = requests.get(url, params=params)
    return response.json()
```

**Ограничения:**
- Бесплатный tier: 100 запросов/день
- Платный: от $449/месяц

#### Alpha Vantage News & Sentiment
**URL:** `https://www.alphavantage.co/documentation/#news-sentiment`

**Подключение:**
```python
def fetch_alphavantage_news(api_key, tickers='MSFT,AMZN'):
    """
    Получает новости и sentiment через Alpha Vantage
    
    Args:
        api_key: API ключ (бесплатный, лимиты)
        tickers: Список тикеров через запятую
    """
    url = "https://www.alphavantage.co/query"
    params = {
        'function': 'NEWS_SENTIMENT',
        'tickers': tickers,
        'apikey': api_key
    }
    
    response = requests.get(url, params=params)
    return response.json()
```

### 5. Institutional Forecasts

#### Bloomberg Terminal API (платный)
**Стоимость:** Очень высокая ($2000+/месяц)

**Альтернативы:**
- Парсинг публичных отчетов Goldman Sachs, JPMorgan (PDF/HTML)
- RSS фиды от крупных банков (если доступны)
- Twitter/X API для отслеживания аналитиков

#### Financial Times RSS
**URL:** `https://www.ft.com/?format=rss`

#### Reuters RSS
**URL:** `https://www.reuters.com/rssFeed`

---

## 🤖 Метод использования для торговых рекомендаций

### Расширенный LLM анализ новостей

**Текущий промпт** (`services/sentiment_analyzer.py`) возвращает только:
- `sentiment`: 0.0-1.0
- `insight`: краткий факт

**Новый промпт** должен возвращать структурированный `impact_json`:

```python
def analyze_news_impact(content: str, event_type: str, region: str = None) -> dict:
    """
    Анализирует новость и предсказывает impact на различные активы
    
    Returns:
        dict: Структурированный impact_json (см. выше)
    """
    from services.llm_service import get_llm_service
    import json
    
    llm_service = get_llm_service()
    
    system_prompt = """Ты опытный макроэкономический аналитик и трейдер.
Твоя задача - проанализировать новость и предсказать, как она повлияет на различные активы.

ВАЖНО:
1. Учитывай контекст: если доллар стал safe haven (например, после hawkish комментариев FRS),
   то геополитический риск может поднять доллар, а не золото.
2. Прогнозируй не только направление (UP/DOWN/NEUTRAL), но и уверенность (0.0-1.0) и горизонт (1d, 1-3d, 1w).
3. Для прогнозов институтов (Goldman, JPM) указывай, какие активы будут бенефициарами/пострадавшими.

Отвечай ТОЛЬКО в формате JSON:
{
  "market_impact": {
    "SPY": {"direction": "UP|DOWN|NEUTRAL", "confidence": 0.0-1.0, "horizon": "1-3d"},
    "QQQ": {...}
  },
  "fx_impact": {
    "DXY": {"direction": "UP|DOWN|NEUTRAL", "confidence": 0.0-1.0, "horizon": "1-7d"},
    "EURUSD=X": {...}
  },
  "commodity_impact": {
    "CL=F": {"direction": "UP|DOWN|NEUTRAL", "confidence": 0.0-1.0, "horizon": "1d"},
    "XAUUSD=X": {...}
  },
  "sector_impact": {
    "IT": {"direction": "UP|DOWN|NEUTRAL", "confidence": 0.0-1.0},
    "ENERGY": {...}
  },
  "ticker_impact": {
    "MSFT": {"direction": "UP|DOWN|NEUTRAL", "confidence": 0.0-1.0, "reason": "краткое объяснение"},
    "SNDK": {...}
  },
  "risk_regime": "RISK_ON|RISK_OFF|NEUTRAL",
  "safe_haven": "USD|XAUUSD=X|BONDS|NONE",
  "reasoning": "Подробное объяснение логики прогноза (2-3 предложения)"
}
"""
    
    user_message = f"""Проанализируй следующую новость и предскажи impact на активы:

Тип события: {event_type}
Регион: {region if region else 'N/A'}

Текст новости:
{content}

Дай структурированный прогноз impact."""
    
    messages = [{"role": "user", "content": user_message}]
    
    result = llm_service.generate_response(
        messages,
        system_prompt=system_prompt,
        max_tokens=2000,
        temperature=0.2  # Низкая температура для более детерминированных прогнозов
    )
    
    # Парсинг JSON ответа
    response_text = result["response"].strip()
    try:
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            impact_data = json.loads(json_match.group())
            return impact_data
    except Exception as e:
        logger.error(f"Ошибка парсинга impact_json: {e}")
    
    return {}
```

### Использование impact в AnalystAgent

**Модификация `analyst_agent.py`:**

```python
def get_decision_with_impact(self, ticker: str) -> dict:
    """
    Принимает решение с учетом impact_json из новостей
    """
    # 1. Загружаем новости с impact_json
    news_df = self.get_recent_news(ticker)
    
    # 2. Агрегируем impact по тикеру
    ticker_impacts = []
    for _, row in news_df.iterrows():
        if row.get('impact_json'):
            impact = row['impact_json']
            if 'ticker_impact' in impact and ticker in impact['ticker_impact']:
                ticker_impacts.append(impact['ticker_impact'][ticker])
    
    # 3. Учитываем risk_regime и safe_haven
    risk_regimes = []
    safe_havens = []
    for _, row in news_df.iterrows():
        if row.get('impact_json'):
            impact = row['impact_json']
            if 'risk_regime' in impact:
                risk_regimes.append(impact['risk_regime'])
            if 'safe_haven' in impact:
                safe_havens.append(impact['safe_haven'])
    
    # 4. Комбинируем с техническим анализом
    technical_signal = self.check_technical_signal(ticker)
    
    # 5. Принимаем решение на основе комбинации факторов
    # (логика зависит от стратегии)
    
    return {
        'decision': 'BUY' | 'STRONG_BUY' | 'HOLD',
        'ticker_impacts': ticker_impacts,
        'risk_regime': most_common(risk_regimes),
        'safe_haven': most_common(safe_havens),
        'technical_signal': technical_signal
    }
```

### Отслеживание исполнения прогнозов

**Модуль `services/forecast_tracker.py`:**

```python
def track_forecast_execution(forecast_news_id: int, days_after: int = 7):
    """
    Проверяет, сбылся ли прогноз из новости
    
    Args:
        forecast_news_id: ID новости с прогнозом
        days_after: через сколько дней проверять
    """
    # 1. Загружаем прогноз из knowledge_base
    # 2. Извлекаем impact_json с прогнозами
    # 3. Загружаем фактические цены за период
    # 4. Сравниваем ожидаемое vs фактическое
    # 5. Сохраняем в forecast_tracking
    pass
```

---

## 📅 План реализации (поэтапно)

### Этап 1: Расширение структуры БД (1-2 дня)
- [ ] Миграция для добавления колонок в `knowledge_base`
- [ ] Создание таблицы `forecast_tracking`
- [ ] Обновление `news_importer.py` для поддержки новых полей

### Этап 2: Интеграция источников (3-5 дней)
- [ ] Парсер Investing.com Economic Calendar
- [ ] RSS парсеры для FOMC/BoE/ECB
- [ ] Интеграция Alpha Vantage Earnings Calendar
- [ ] Парсер NewsAPI для новостей

### Этап 3: Расширенный LLM анализ (2-3 дня)
- [ ] Новый промпт для `analyze_news_impact()`
- [ ] Сохранение `impact_json` в БД
- [ ] Обновление `sentiment_analyzer.py`

### Этап 4: Использование в торговых решениях (2-3 дня)
- [ ] Модификация `AnalystAgent.get_decision()` для учета impact
- [ ] Интеграция с `StrategyManager`
- [ ] Обновление `ExecutionAgent` для записи impact в trade_history

### Этап 5: Отслеживание прогнозов (2-3 дня)
- [ ] Модуль `forecast_tracker.py`
- [ ] Автоматическая проверка исполнения прогнозов
- [ ] Метрики точности прогнозов

### Этап 6: Автоматизация и cron (1 день)
- [ ] Скрипт для ежедневного сбора новостей
- [ ] Cron задача для обновления календаря событий
- [ ] Мониторинг и логирование

---

## 🔧 Технические детали

### Зависимости

Добавить в `requirements.txt`:
```
beautifulsoup4>=4.12.0
feedparser>=6.0.10
requests>=2.31.0
selenium>=4.15.0  # Если нужен для сложного парсинга
lxml>=4.9.0  # Для быстрого парсинга XML/HTML
```

### Конфигурация

Добавить в `config.env`:
```env
# News Sources
NEWSAPI_KEY=your_newsapi_key
ALPHAVANTAGE_KEY=your_alphavantage_key
TRADING_ECONOMICS_KEY=your_trading_economics_key  # опционально

# Economic Calendar Regions
ECONOMIC_CALENDAR_REGIONS=USA,UK,EU,Japan,China,Switzerland

# Earnings Tracking
EARNINGS_TRACK_TICKERS=MSFT,SNDK,MU,LITE,ALAB,TER,AAPL,AMZN,GOOGL,META,NFLX,NVDA,TSLA,JPM,BAC,WFC,GS,MS

# Forecast Tracking
FORECAST_TRACKING_ENABLED=true
FORECAST_CHECK_DAYS=7  # Проверять исполнение через N дней
```

### Примеры использования

**Добавление новости с impact:**
```python
from news_importer import add_news
from services.sentiment_analyzer import analyze_news_impact

content = "Fed officials signal potential rate hike, dollar surges..."
impact = analyze_news_impact(content, event_type='FOMC_SPEECH', region='USA')

add_news(
    engine,
    ticker='US_MACRO',
    source='Reuters',
    content=content,
    event_type='FOMC_SPEECH',
    region='USA',
    importance='HIGH',
    impact_json=impact
)
```

**Использование impact в анализе:**
```python
from analyst_agent import AnalystAgent

agent = AnalystAgent()
decision = agent.get_decision_with_impact('MSFT')

print(f"Decision: {decision['decision']}")
print(f"Risk Regime: {decision['risk_regime']}")
print(f"Safe Haven: {decision['safe_haven']}")
```

---

## 📊 Метрики успеха

1. **Покрытие must-list источников**: 100% обязательных категорий
2. **Точность прогнозов impact**: отслеживание через `forecast_tracking`
3. **Время реакции**: новости попадают в БД в течение 1 часа после публикации
4. **Качество торговых решений**: улучшение Sharpe ratio на 10-15%

---

## 🔗 Ссылки и ресурсы

- [Investing.com Economic Calendar](https://www.investing.com/economic-calendar/)
- [Alpha Vantage API](https://www.alphavantage.co/documentation/)
- [NewsAPI](https://newsapi.org/)
- [Federal Reserve RSS Feeds](https://www.federalreserve.gov/feeds/)
- [EarningsWhispers](https://www.earningswhispers.com/)
- [Trading Economics API](https://tradingeconomics.com/api)

---

**Статус:** План готов к реализации  
**Последнее обновление:** 2026-02-19

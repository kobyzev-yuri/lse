# Формат тикеров в системе

## Общие правила

Система использует формат тикеров Yahoo Finance (yfinance), так как данные загружаются через эту библиотеку.

## Типы тикеров

### 1. Акции (Stocks)

**Формат**: Простой код без суффиксов

**Примеры**:
- `MSFT` - Microsoft Corporation
- `SNDK` - SanDisk Corporation (теперь часть Western Digital)
- `AAPL` - Apple Inc.
- `GOOGL` - Alphabet Inc. (Class A)
- `TSLA` - Tesla Inc.

**Использование**:
```bash
python update_prices.py MSFT,AAPL,TSLA
```

### 2. Валютные пары (Forex/Currency Pairs)

**Формат**: `[BASE][QUOTE]=X`

Где:
- `BASE` - базовая валюта (валюта, которую покупаем/продаем)
- `QUOTE` - котируемая валюта (валюта, в которой выражается цена)
- `=X` - обязательный суффикс для валютных пар в Yahoo Finance

**Примеры**:
- `GBPUSD=X` - британский фунт стерлингов к доллару США (GBP/USD)
- `EURUSD=X` - евро к доллару США (EUR/USD)
- `USDJPY=X` - доллар США к японской иене (USD/JPY)
- `GBPEUR=X` - британский фунт к евро (GBP/EUR)
- `USDCHF=X` - доллар США к швейцарскому франку (USD/CHF)

**Важно**: 
- Это **GBPUSD**, а не GBRUSD (GBP = Great Britain Pound)
- Суффикс `=X` обязателен для валютных пар в Yahoo Finance
- Без `=X` система будет искать акцию с таким тикером, что приведет к ошибке

**Использование**:
```bash
python update_prices.py GBPUSD=X,EURUSD=X
```

### 3. Криптовалюты (Cryptocurrencies)

**Формат**: `[CRYPTO]-USD` или `[CRYPTO]USD=X`

**Примеры**:
- `BTC-USD` - Bitcoin к доллару США
- `ETH-USD` - Ethereum к доллару США
- `BTCUSD=X` - альтернативный формат для Bitcoin

**Использование**:
```bash
python update_prices.py BTC-USD,ETH-USD
```

### 4. Индексы (Indices)

**Формат**: Обычно с префиксом `^`

**Примеры**:
- `^GSPC` - S&P 500 Index
- `^DJI` - Dow Jones Industrial Average
- `^IXIC` - NASDAQ Composite
- `^FTSE` - FTSE 100 Index (Лондонская биржа)

**Использование**:
```bash
python update_prices.py ^GSPC,^DJI
```

### 5. Товары (Commodities)

**Формат**: Различные форматы в зависимости от товара

**Примеры**:
- `GC=F` - Gold Futures
- `CL=F` - Crude Oil Futures
- `SI=F` - Silver Futures

**Использование**:
```bash
python update_prices.py GC=F,CL=F
```

## Почему GBPUSD, а не GBRUSD?

**GBP** = **G**reat **B**ritain **P**ound (британский фунт стерлингов)

Это стандартный ISO 4217 код валюты:
- **GBP** - правильный код для британского фунта
- **GBR** - это код страны (ISO 3166-1 alpha-3), а не валюты

В финансовых системах всегда используется **GBP** для обозначения британского фунта.

## Что означает =X?

Суффикс `=X` в Yahoo Finance указывает, что это **валютная пара (Forex)**, а не акция.

**История**: Yahoo Finance использует различные суффиксы для разных типов инструментов:
- `=X` - валютные пары (Forex)
- `=F` - фьючерсы (Futures)
- `-USD` - криптовалюты к доллару
- `^` - индексы
- Без суффикса - акции

**Примеры различий**:
```python
# Валютная пара GBP/USD
yf.download("GBPUSD=X")  # ✅ Правильно

# Попытка найти акцию GBPUSD (не существует)
yf.download("GBPUSD")     # ❌ Ошибка или неправильные данные
```

## Проверка формата тикера

Если вы не уверены в формате тикера, можно проверить на сайте Yahoo Finance:
1. Перейдите на https://finance.yahoo.com/
2. Введите тикер в поиск
3. Проверьте формат в URL или на странице

Или используйте yfinance для проверки:
```python
import yfinance as yf

# Проверка тикера
ticker = "GBPUSD=X"
data = yf.download(ticker, period="5d")
if not data.empty:
    print(f"✅ Тикер {ticker} корректен")
else:
    print(f"❌ Тикер {ticker} не найден")
```

## Рекомендации для системы LSE Trading

Для торговли на Лондонской бирже (LSE) наиболее релевантны:

**Акции LSE**:
- Используйте формат Yahoo Finance (обычно с суффиксом `.L` для Лондонской биржи):
  - `VOD.L` - Vodafone Group
  - `BP.L` - BP plc
  - `HSBA.L` - HSBC Holdings

**Валютные пары**:
- `GBPUSD=X` - фунт к доллару (основная пара для LSE)
- `GBPEUR=X` - фунт к евро
- `EURUSD=X` - евро к доллару

**Индексы**:
- `^FTSE` - FTSE 100 (основной индекс LSE)

## Примеры использования в системе

```bash
# Обновление акций LSE
python update_prices.py VOD.L,BP.L,HSBA.L

# Обновление валютных пар
python update_prices.py GBPUSD=X,GBPEUR=X,EURUSD=X

# Смешанный список
python update_prices.py MSFT,SNDK,GBPUSD=X,^FTSE
```

---

**Последнее обновление**: 2026-02-13


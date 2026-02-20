# Интеграция с Finviz.com

## Обзор

Вместо самостоятельного расчета технических индикаторов (например, RSI), система теперь использует данные с проверенного ресурса **Finviz.com**. Это экономит время и вычислительные ресурсы, а также обеспечивает использование профессионально рассчитанных индикаторов.

**Важно:** Finviz даёт RSI только по **акциям США** (MSFT, AAPL и т.д.). Для **валютных пар** (GBPUSD=X, EURUSD=X) и **товаров** (GC=F) RSI нужно загружать через **Alpha Vantage** (технические индикаторы). Скрипт `update_finviz_data.py` для пар вроде GBPUSD=X ничего не добавит.

## Что реализовано

### 1. Модуль парсинга Finviz

**Файл:** `services/finviz_parser.py`

Модуль предоставляет класс `FinvizParser` для получения данных с Finviz:

- **RSI для конкретного тикера** - получает значение RSI (0-100) для указанной акции
- **Перепроданные стоки** - получает список акций с RSI ниже заданного порога
- **Технические индикаторы** - получает все доступные индикаторы для тикера

### 2. Обновление базы данных

**Таблица `quotes`** теперь содержит поле `rsi`:
```sql
rsi DECIMAL(5,2)  -- RSI из Finviz (0-100)
```

### 3. Скрипт обновления RSI

**Файл:** `update_finviz_data.py`

Скрипт для обновления RSI в базе данных из Finviz.

## Как часто обновлять

| Источник        | Что обновляет      | Рекомендуемая частота      |
|-----------------|--------------------|-----------------------------|
| Finviz          | RSI по акциям США  | 1 раз в день (утром/вечер)  |
| Alpha Vantage   | RSI по всем (в т.ч. валюты, товары) | 1 раз в день с учётом лимитов API (25 запросов/день на free tier) |

Для валют (GBPUSD=X, GC=F) запускайте загрузку индикаторов через Alpha Vantage, а не `update_finviz_data.py`.

## Использование

### Обновление RSI для всех тикеров (только акции)

```bash
python update_finviz_data.py
```

Обновит RSI для всех тикеров, отслеживаемых в системе.

### Обновление RSI для конкретных тикеров

```bash
python update_finviz_data.py MSFT,AAPL,TSLA
```

### Получение перепроданных стоков

```bash
python update_finviz_data.py --oversold NYSE 30
```

Параметры:
- `NYSE` - биржа (NYSE, NASDAQ, AMEX)
- `30` - максимальное значение RSI для перепроданности

### Использование в Python коде

```python
from services.finviz_parser import FinvizParser, get_rsi_for_tickers

# Получение RSI для одного тикера
parser = FinvizParser()
rsi = parser.get_rsi_for_ticker("MSFT")
print(f"MSFT RSI: {rsi}")

# Получение RSI для нескольких тикеров
rsi_data = get_rsi_for_tickers(["MSFT", "AAPL", "TSLA"])
print(rsi_data)  # {'MSFT': 45.5, 'AAPL': 52.3, 'TSLA': 38.2}

# Получение перепроданных стоков
oversold = parser.get_oversold_stocks(exchange='NYSE', min_rsi=30.0)
for stock in oversold:
    print(f"{stock['ticker']}: RSI={stock['rsi']}")
```

## Интеграция с существующими скриптами

### Автоматическое обновление через cron

Добавьте в cron задачу для обновления RSI:

```bash
# Обновление RSI ежедневно в 19:00 (после обновления цен)
0 19 * * 1-5 cd /mnt/ai/cnn/lse && /usr/bin/python3 update_finviz_data.py >> logs/update_finviz.log 2>&1
```

### Использование RSI в анализе

RSI теперь доступен в таблице `quotes` и может использоваться в стратегиях:

```python
from sqlalchemy import create_engine, text

engine = create_engine(db_url)
with engine.connect() as conn:
    result = conn.execute(text("""
        SELECT ticker, close, rsi
        FROM quotes
        WHERE ticker = :ticker
        ORDER BY date DESC
        LIMIT 1
    """), {"ticker": "MSFT"})
    
    row = result.fetchone()
    if row:
        ticker, close, rsi = row
        if rsi and rsi < 30:
            print(f"{ticker} перепродан (RSI={rsi})")
        elif rsi and rsi > 70:
            print(f"{ticker} перекуплен (RSI={rsi})")
```

## Преимущества

1. **Экономия ресурсов** - не нужно рассчитывать RSI самостоятельно
2. **Проверенные данные** - используем данные с доверенного ресурса
3. **Актуальность** - данные обновляются с Finviz в реальном времени
4. **Гибкость** - можно получать различные технические индикаторы
5. **Перепроданные стоки** - легко находить возможности для покупки

## Ограничения

- **Rate limiting** - между запросами есть задержка (по умолчанию 1-1.5 секунды) для избежания блокировки
- **Зависимость от Finviz** - если Finviz недоступен, RSI не обновится
- **Структура HTML** - если Finviz изменит структуру страниц, парсер может потребовать обновления

## Настройка

В `services/finviz_parser.py` можно настроить:

- `delay` - задержка между запросами (по умолчанию 1.0 секунда)
- `User-Agent` - заголовок для запросов (можно изменить для обхода блокировок)

## Примеры использования в стратегиях

### Mean Reversion Strategy с RSI

```python
# В стратегии можно использовать RSI из базы данных
def get_signal(self, ticker, quotes_df, sentiment_score):
    # Получаем последний RSI
    last_rsi = quotes_df['rsi'].iloc[-1] if 'rsi' in quotes_df.columns else None
    
    if last_rsi and last_rsi < 30:
        # Перепроданность - сигнал на покупку для Mean Reversion
        return "STRONG_BUY"
    elif last_rsi and last_rsi > 70:
        # Перекупленность - сигнал на продажу
        return "STRONG_SELL"
    
    # ... остальная логика
```

## См. также

- [TRADING_GLOSSARY.md](TRADING_GLOSSARY.md) - описание RSI и других индикаторов
- [update_prices.py](../update_prices.py) - обновление цен котировок
- [analyst_agent.py](../analyst_agent.py) - использование данных в анализе


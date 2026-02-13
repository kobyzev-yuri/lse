# Формат тикеров в системе

Система использует формат тикеров Yahoo Finance (yfinance).

## Типы тикеров

### Акции (Stocks)
**Формат**: Простой код без суффиксов

**Примеры**: `MSFT`, `SNDK`, `AAPL`, `TSLA`

```bash
python update_prices.py MSFT,AAPL,TSLA
```

### Валютные пары (Forex)
**Формат**: `[BASE][QUOTE]=X`

**Примеры**:
- `GBPUSD=X` - британский фунт к доллару (GBP/USD)
- `EURUSD=X` - евро к доллару (EUR/USD)
- `USDJPY=X` - доллар к иене (USD/JPY)

**Важно**: Суффикс `=X` обязателен для валютных пар. Без него система будет искать акцию.

```bash
python update_prices.py GBPUSD=X,EURUSD=X
```

### Криптовалюты
**Формат**: `[CRYPTO]-USD` или `[CRYPTO]USD=X`

**Примеры**: `BTC-USD`, `ETH-USD`, `BTCUSD=X`

### Индексы
**Формат**: С префиксом `^`

**Примеры**: `^GSPC` (S&P 500), `^DJI` (Dow Jones), `^FTSE` (FTSE 100)

### Товары
**Формат**: С суффиксом `=F`

**Примеры**: `GC=F` (Gold), `CL=F` (Crude Oil), `SI=F` (Silver)

## Суффиксы Yahoo Finance

- `=X` - валютные пары (Forex)
- `=F` - фьючерсы (Futures)
- `-USD` - криптовалюты к доллару
- `^` - индексы
- Без суффикса - акции

## Рекомендации для LSE Trading

**Акции LSE**: Суффикс `.L` (например, `VOD.L`, `BP.L`, `HSBA.L`)

**Валютные пары**: `GBPUSD=X`, `GBPEUR=X`, `EURUSD=X`

**Индексы**: `^FTSE` (FTSE 100)

## Примеры использования

```bash
# Акции LSE
python update_prices.py VOD.L,BP.L,HSBA.L

# Валютные пары
python update_prices.py GBPUSD=X,GBPEUR=X

# Смешанный список
python update_prices.py MSFT,SNDK,GBPUSD=X,^FTSE
```

## Проверка тикера

Если не уверены в формате, проверьте на https://finance.yahoo.com/ или используйте:

```python
import yfinance as yf
data = yf.download("GBPUSD=X", period="5d")
if not data.empty:
    print("✅ Тикер корректен")
```

---

**Последнее обновление**: 2026-02-13

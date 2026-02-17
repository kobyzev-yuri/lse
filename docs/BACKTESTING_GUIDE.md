# Руководство по бэктестингу на исторических данных

## Возможности

Система поддерживает **бэктестинг на исторических данных**, что позволяет:
- ✅ Симулировать торговлю на прошлых данных
- ✅ Тестировать стратегии без риска
- ✅ Анализировать эффективность разных стратегий
- ✅ Оценивать PnL и Win Rate на исторических данных

---

## Быстрый старт

### 1. Базовый пример

```python
from datetime import datetime, timedelta
from backtest_engine import BacktestEngine

# Создаем движок бэктестинга
engine = BacktestEngine(initial_cash=100_000.0)

# Определяем период (последние 3 месяца)
end_date = datetime.now()
start_date = end_date - timedelta(days=90)

# Запускаем бэктестинг
results = engine.run_backtest(
    tickers=["MSFT", "SNDK"],
    start_date=start_date,
    end_date=end_date,
    use_llm=False,  # Отключаем LLM для скорости
    reset_before=True  # Сбросить портфель перед началом
)

print(f"PnL: ${results['total_pnl']:,.2f} ({results['pnl_percent']:.2f}%)")
print(f"Win Rate: {results['win_rate']:.2f}%")
```

### 2. Запуск готового примера

```bash
python examples/backtest_example.py
```

---

## Как это работает

### Принцип работы

1. **Инициализация**: Создается `BacktestEngine` с начальным капиталом
2. **Сброс портфеля**: Портфель сбрасывается к начальному состоянию
3. **Итерация по датам**: Система проходит по каждой торговой дате в диапазоне
4. **Анализ и исполнение**: Для каждой даты:
   - `AnalystAgent` анализирует ситуацию (используя данные до текущей даты)
   - `ExecutionAgent` исполняет сделки по ценам на текущую дату
5. **Результаты**: Рассчитывается финальный PnL, Win Rate и статистика

### Важные особенности

- ✅ **Нет look-ahead bias**: Система использует только данные до текущей даты
- ✅ **Реалистичные цены**: Используются реальные цены закрытия из БД
- ✅ **Учет комиссий**: Комиссии учитываются при расчете PnL
- ✅ **Стоп-лоссы**: Стоп-лоссы проверяются на каждой дате
- ✅ **История сделок**: Все сделки записываются в `trade_history`

---

## Параметры бэктестинга

### `run_backtest()`

```python
results = engine.run_backtest(
    tickers=["MSFT", "SNDK"],           # Список тикеров
    start_date=datetime(2025, 1, 1),    # Начальная дата
    end_date=datetime(2025, 12, 31),    # Конечная дата
    use_llm=False,                      # Использовать LLM (медленнее, но точнее)
    reset_before=True                   # Сбросить портфель перед началом
)
```

**Параметры:**
- `tickers`: Список тикеров для тестирования
- `start_date`: Начальная дата бэктестинга
- `end_date`: Конечная дата бэктестинга
- `use_llm`: Использовать LLM анализ (по умолчанию `False` для скорости)
- `reset_before`: Сбросить портфель перед началом (по умолчанию `True`)

---

## Результаты бэктестинга

### Структура результатов

```python
{
    'initial_cash': 100000.0,           # Начальный капитал
    'final_cash': 105000.0,             # Финальный баланс
    'open_positions_value': 5000.0,     # Стоимость открытых позиций
    'total_value': 110000.0,            # Общая стоимость портфеля
    'total_pnl': 10000.0,               # Общий PnL
    'pnl_percent': 10.0,                # PnL в процентах
    'closed_pnl': 8000.0,               # PnL по закрытым сделкам
    'win_rate': 65.5,                   # Win Rate в процентах
    'trades_count': 25,                 # Всего сделок
    'decisions_count': 180,             # Всего решений
    'dates_processed': 180,             # Обработано дат
    'closed_trades_count': 20          # Закрытых сделок
}
```

---

## Примеры использования

### Пример 1: Бэктестинг за последние 6 месяцев

```python
from datetime import datetime, timedelta
from backtest_engine import BacktestEngine

engine = BacktestEngine(initial_cash=100_000.0)
end_date = datetime.now()
start_date = end_date - timedelta(days=180)

results = engine.run_backtest(
    tickers=["MSFT", "SNDK"],
    start_date=start_date,
    end_date=end_date,
    use_llm=False
)

print(f"PnL: {results['pnl_percent']:.2f}%")
```

### Пример 2: Бэктестинг с LLM анализом

```python
results = engine.run_backtest(
    tickers=["MSFT", "SNDK"],
    start_date=start_date,
    end_date=end_date,
    use_llm=True  # Включаем LLM для более точного анализа
)
```

**Внимание:** С LLM бэктестинг работает значительно медленнее из-за API вызовов.

### Пример 3: Бэктестинг конкретного периода

```python
from datetime import datetime

results = engine.run_backtest(
    tickers=["MSFT", "SNDK"],
    start_date=datetime(2025, 1, 1),
    end_date=datetime(2025, 3, 31),
    use_llm=False
)
```

---

## Анализ эффективности стратегий

После бэктестинга можно проанализировать, какая стратегия показала лучшие результаты:

```python
from sqlalchemy import create_engine, text
from config_loader import get_database_url
import pandas as pd

db_url = get_database_url()
engine = create_engine(db_url)

with engine.connect() as conn:
    strategy_stats = pd.read_sql(
        text("""
            SELECT 
                strategy_name,
                COUNT(*) as trade_count,
                SUM(CASE WHEN side = 'BUY' THEN -total_value ELSE total_value END) as net_pnl,
                AVG(sentiment_at_trade) as avg_sentiment
            FROM trade_history
            WHERE strategy_name IS NOT NULL
            GROUP BY strategy_name
            ORDER BY net_pnl DESC
        """),
        conn
    )
    
    print(strategy_stats)
```

---

## Ограничения и особенности

### 1. Look-ahead bias
- ✅ Система использует только данные до текущей даты
- ✅ Нет доступа к будущим данным

### 2. Исполнение сделок
- ⚠️ Используется цена закрытия на текущую дату
- ⚠️ В реальности может быть проскальзывание (slippage)
- ⚠️ Нет учета ликвидности

### 3. Новости и sentiment
- ⚠️ Используются новости, которые были в БД на момент бэктестинга
- ⚠️ Если новостей нет, sentiment = 0.0

### 4. Комиссии
- ✅ Учитываются комиссии (0.1% от номинала)
- ✅ Реалистично для LSE

---

## Рекомендации

### Для быстрого тестирования:
- Используйте `use_llm=False`
- Тестируйте на периоде 1-3 месяца
- Используйте 1-2 тикера

### Для точного анализа:
- Используйте `use_llm=True`
- Тестируйте на периоде 6-12 месяцев
- Добавьте новости в БД для более реалистичного sentiment анализа

### Для сравнения стратегий:
- Запускайте бэктестинг с `reset_before=True` для каждого теста
- Анализируйте результаты через SQL запросы
- Сравнивайте Win Rate и PnL по стратегиям

---

## Интеграция с веб-интерфейсом

В будущем можно добавить страницу бэктестинга в веб-интерфейс:
- Выбор периода
- Выбор тикеров
- Запуск бэктестинга
- Визуализация результатов

---

## См. также

- [backtest_engine.py](../backtest_engine.py) - движок бэктестинга
- [examples/backtest_example.py](../examples/backtest_example.py) - пример использования
- [DEBUGGING_GUIDE.md](DEBUGGING_GUIDE.md) - руководство по отладке
- [TRADING_GLOSSARY.md](TRADING_GLOSSARY.md) - терминология (Backtesting)




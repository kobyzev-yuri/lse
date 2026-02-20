# Сводка: Фабрика стратегий (Strategy Factory)

## ✅ Что реализовано

### 1. Файл `strategies.py`

Создан модуль с паттерном Strategy:

- **`BaseStrategy`** - абстрактный базовый класс для всех стратегий
- **`MomentumStrategy`** - стратегия следования тренду
- **`MeanReversionStrategy`** - стратегия возврата к среднему
- **`VolatileGapStrategy`** - стратегия для волатильных рынков с гэпами
- **`NeutralStrategy`** - нейтральный режим (fallback, когда ни одна не подходит)
- **`StrategyManager`** - диспетчер для автоматического выбора стратегии

### 2. Интеграция в AnalystAgent

- Автоматический выбор стратегии на основе:
  - Волатильности (из PostgreSQL)
  - Новостей (из PostgreSQL)
  - Sentiment анализа
- Метод `get_decision()` использует фабрику стратегий
- Метод `get_decision_with_llm()` комбинирует стратегии с LLM анализом

### 3. Обновление веб-интерфейса

Веб-интерфейс теперь отображает:
- Выбранную стратегию
- Сигнал от стратегии
- Рекомендуемые параметры (стоп-лосс, тейк-профит)

## Доступные стратегии

### MomentumStrategy
- **Условия**: Цена выше SMA, низкая волатильность
- **Стоп-лосс**: 3%
- **Тейк-профит**: 8%

### MeanReversionStrategy
- **Условия**: Значительное отклонение от среднего (>2%), высокая волатильность
- **Стоп-лосс**: 5%
- **Тейк-профит**: 4%

### VolatileGapStrategy
- **Условия**: Очень высокая волатильность (>1.5x), гэп или экстремальный sentiment
- **Стоп-лосс**: 7%
- **Тейк-профит**: 12%

### NeutralStrategy
- **Условия**: Fallback — когда ни одна стратегия не подходит (режим не определён)
- **Сигнал**: HOLD
- **Назначение**: валюты/боковик, консервативная рекомендация

## Использование

### Автоматический выбор (рекомендуется)

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_strategy_factory=True)
decision = agent.get_decision("MSFT")  # Автоматически выберет стратегию
```

### Детальный анализ

```python
result = agent.get_decision_with_llm("MSFT")
print(result['selected_strategy'])  # Название выбранной стратегии
print(result['strategy_result'])    # Детали сигнала
```

### Прямое использование фабрики

```python
from strategies import get_strategy_factory

factory = get_strategy_factory()
strategy = factory.select_strategy(technical_data, news_data, sentiment_score)
```

## Добавление новой стратегии

1. Создать класс, наследующий `BaseStrategy`
2. Реализовать методы `is_suitable()` и `calculate_signal()`
3. Добавить в список стратегий в `StrategyFactory.__init__()`

Пример см. в [docs/STRATEGY_FACTORY.md](docs/STRATEGY_FACTORY.md)

## Файлы

- `strategies.py` - реализация фабрики стратегий
- `analyst_agent.py` - интеграция с AnalystAgent
- `templates/trading.html` - обновлен веб-интерфейс
- `docs/STRATEGY_FACTORY.md` - подробная документация
- `examples/strategy_example.py` - примеры использования

## Преимущества

1. **Гибкость** - легко добавлять новые стратегии
2. **Автоматизация** - выбор стратегии на основе данных из PostgreSQL
3. **Расширяемость** - паттерн Strategy позволяет легко расширять систему
4. **Тестируемость** - каждая стратегия может тестироваться отдельно

## Следующие шаги

1. Добавить больше стратегий (например, BreakoutStrategy, RangeStrategy)
2. Реализовать систему приоритетов для выбора стратегий
3. Добавить метрики эффективности для каждой стратегии
4. Интегрировать параметры стратегий в ExecutionAgent




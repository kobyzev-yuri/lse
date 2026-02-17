# Фабрика стратегий (Strategy Factory)

## Описание

Фабрика стратегий реализует паттерн Strategy для динамического выбора оптимальной торговой стратегии на основе текущих рыночных условий (волатильность, новости, sentiment).

## Архитектура

### Базовый класс: `BaseStrategy`

Все стратегии наследуются от `BaseStrategy` и реализуют два основных метода:

1. **`is_suitable()`** - проверяет, подходит ли стратегия для текущих условий
2. **`calculate_signal()`** - вычисляет торговый сигнал

### Доступные стратегии

#### 1. MomentumStrategy (Следование тренду)

**Когда используется:**
- Цена выше SMA (восходящий тренд)
- Низкая волатильность относительно среднего
- Положительный sentiment новостей

**Характеристики:**
- Стоп-лосс: 3%
- Тейк-профит: 8%
- Подходит для стабильных трендовых рынков

#### 2. MeanReversionStrategy (Возврат к среднему)

**Когда используется:**
- Значительное отклонение цены от среднего (>2%)
- Высокая волатильность
- Нейтральный или противоречивый sentiment

**Характеристики:**
- Стоп-лосс: 5%
- Тейк-профит: 4%
- Подходит для волатильных рынков с перекупленностью/перепроданностью

#### 3. VolatileGapStrategy (Волатильные гэпы)

**Когда используется:**
- Очень высокая волатильность (>1.5x от среднего)
- Важные новости (макро-события)
- Экстремальный sentiment (>0.8 или <0.2)

**Характеристики:**
- Стоп-лосс: 7%
- Тейк-профит: 12%
- Подходит для нестабильных рынков с важными событиями

## Использование

### Автоматический выбор стратегии

```python
from analyst_agent import AnalystAgent

# AnalystAgent автоматически использует фабрику стратегий
agent = AnalystAgent(use_strategy_factory=True)

# Базовый анализ с автоматическим выбором стратегии
decision = agent.get_decision("MSFT")
print(decision)  # BUY, STRONG_BUY, HOLD, SELL

# Расширенный анализ с информацией о стратегии
result = agent.get_decision_with_llm("MSFT")
print(result['selected_strategy'])  # Momentum, Mean Reversion, Volatile Gap
print(result['strategy_result'])  # Детали сигнала от стратегии
```

### Прямое использование фабрики

```python
from strategies import get_strategy_factory
from analyst_agent import AnalystAgent

agent = AnalystAgent()
strategy_factory = get_strategy_factory()

# Подготовка данных
technical_data = {
    "close": 350.0,
    "sma_5": 345.0,
    "volatility_5": 2.5,
    "avg_volatility_20": 3.0,
    "technical_signal": "BUY"
}

news_data = [
    {"source": "Reuters", "content": "...", "sentiment_score": 0.7}
]

sentiment_score = 0.75

# Выбор стратегии
selected_strategy = strategy_factory.select_strategy(
    technical_data=technical_data,
    news_data=news_data,
    sentiment_score=sentiment_score
)

if selected_strategy:
    # Вычисление сигнала
    result = selected_strategy.calculate_signal(
        ticker="MSFT",
        technical_data=technical_data,
        news_data=news_data,
        sentiment_score=sentiment_score
    )
    
    print(f"Стратегия: {selected_strategy.name}")
    print(f"Сигнал: {result['signal']}")
    print(f"Уверенность: {result['confidence']}")
    print(f"Стоп-лосс: {result['stop_loss']}%")
    print(f"Тейк-профит: {result['take_profit']}%")
```

## Добавление новой стратегии

### Шаг 1: Создать класс стратегии

```python
from strategies import BaseStrategy

class MyCustomStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("My Custom Strategy")
    
    def is_suitable(self, technical_data, news_data, sentiment_score):
        # Логика проверки условий
        return True  # или False
    
    def calculate_signal(self, ticker, technical_data, news_data, sentiment_score):
        # Логика вычисления сигнала
        return {
            "signal": "BUY",
            "confidence": 0.8,
            "reasoning": "Обоснование",
            "entry_price": technical_data.get('close'),
            "stop_loss": 5.0,
            "take_profit": 10.0,
            "strategy": self.name
        }
```

### Шаг 2: Зарегистрировать в фабрике

```python
# В strategies.py, в методе __init__ класса StrategyFactory
self.strategies = [
    MomentumStrategy(),
    MeanReversionStrategy(),
    VolatileGapStrategy(),
    MyCustomStrategy()  # Добавить новую стратегию
]
```

## Интеграция с AnalystAgent

Фабрика стратегий автоматически интегрирована в `AnalystAgent`:

1. **В методе `get_decision()`**:
   - Выбирает подходящую стратегию на основе условий
   - Использует стратегию для вычисления сигнала
   - Fallback к базовой логике, если стратегия не выбрана

2. **В методе `get_decision_with_llm()`**:
   - Выбирает стратегию
   - Комбинирует с LLM анализом
   - Возвращает полную информацию о стратегии и сигнале

## Формат результата стратегии

```python
{
    "signal": "BUY" | "STRONG_BUY" | "HOLD" | "SELL",
    "confidence": 0.0-1.0,
    "reasoning": "детальное обоснование",
    "entry_price": float | None,
    "stop_loss": float,  # в процентах
    "take_profit": float,  # в процентах
    "strategy": "название стратегии"
}
```

## Приоритет выбора стратегии

Если несколько стратегий подходят для текущих условий, выбирается первая подходящая в порядке:
1. MomentumStrategy
2. MeanReversionStrategy
3. VolatileGapStrategy

Можно добавить систему приоритетов или взвешивания для более точного выбора.

## Логирование

Фабрика стратегий логирует:
- Инициализацию всех стратегий
- Выбор стратегии для конкретных условий
- Результаты вычисления сигналов

## Примеры использования

### Пример 1: Проверка всех стратегий

```python
from strategies import get_strategy_factory

factory = get_strategy_factory()

# Получить все стратегии
all_strategies = factory.get_all_strategies()
for strategy in all_strategies:
    print(f"Стратегия: {strategy.name}")
```

### Пример 2: Получение стратегии по имени

```python
from strategies import get_strategy_factory

factory = get_strategy_factory()
momentum = factory.get_strategy_by_name("Momentum")

if momentum:
    result = momentum.calculate_signal(...)
```

### Пример 3: Использование в ExecutionAgent

```python
from execution_agent import ExecutionAgent
from analyst_agent import AnalystAgent

analyst = AnalystAgent(use_strategy_factory=True)
executor = ExecutionAgent()

result = analyst.get_decision_with_llm("MSFT")

if result['strategy_result']:
    strategy = result['strategy_result']
    if strategy['signal'] in ['BUY', 'STRONG_BUY']:
        # Использовать параметры от стратегии
        print(f"Стоп-лосс: {strategy['stop_loss']}%")
        print(f"Тейк-профит: {strategy['take_profit']}%")
        executor._execute_buy("MSFT", strategy['signal'])
```

## См. также

- [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md) - Бизнес-процессы системы
- [LLM_GUIDANCE.md](LLM_GUIDANCE.md) - LLM анализ для выбора стратегии
- [examples/strategy_example.py](../examples/strategy_example.py) - Примеры использования




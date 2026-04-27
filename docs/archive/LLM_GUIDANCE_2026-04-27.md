# LLM Guidance для выбора торговой стратегии

## Описание

Метод `get_llm_guidance()` в `AnalystAgent` использует LLM для выбора оптимальной торговой стратегии на основе технических данных и контекста новостей.

## Доступные стратегии

### 1. Mean Reversion (Возврат к среднему)
**Когда использовать:**
- Цена значительно отклонилась от среднего значения
- Высокая волатильность, но ожидается возврат к норме
- Рынок перекуплен или перепродан
- Новости нейтральные или противоречивые

**Характеристики:**
- Торговля против тренда
- Ожидание возврата цены к среднему
- Подходит для волатильных рынков

### 2. Momentum (Следование тренду)
**Когда использовать:**
- Явный восходящий или нисходящий тренд
- Цена выше/ниже скользящей средней
- Низкая волатильность, стабильный тренд
- Положительные/отрицательные новости подтверждают тренд

**Характеристики:**
- Торговля по тренду
- Следование за импульсом
- Подходит для трендовых рынков

### 3. Hold (Удержание)
**Когда использовать:**
- Неопределенность в технических индикаторах
- Противоречивые сигналы
- Высокая неопределенность в новостях
- Рынок в консолидации

**Характеристики:**
- Отсутствие сделки или удержание текущей позиции
- Ожидание более четких сигналов

## Использование

### Базовое использование

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=True)

# Получаем расширенный анализ с LLM guidance
result = agent.get_decision_with_llm("MSFT")

# Доступ к стратегии
strategy = result['llm_guidance']['strategy']  # "Mean Reversion", "Momentum", или "Hold"
reasoning = result['llm_guidance']['reasoning']
confidence = result['llm_guidance']['confidence']
```

### Прямой вызов метода

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=True)

# Подготовка данных
tech_data = {
    "close": 350.0,
    "sma_5": 345.0,
    "volatility_5": 2.5,
    "avg_volatility_20": 3.0,
    "technical_signal": "BUY"
}

news_context = [
    {
        "source": "Reuters",
        "content": "Microsoft announces new product...",
        "sentiment_score": 0.7
    }
]

# Получаем guidance
guidance = agent.get_llm_guidance(
    ticker="MSFT",
    tech_data=tech_data,
    news_context=news_context
)

print(f"Стратегия: {guidance['strategy']}")
print(f"Обоснование: {guidance['reasoning']}")
print(f"Уверенность: {guidance['confidence']}")
```

## Формат ответа

Метод возвращает словарь со следующей структурой:

```python
{
    "strategy": "Mean Reversion" | "Momentum" | "Hold",
    "reasoning": "детальное обоснование выбора стратегии",
    "confidence": 0.0-1.0,  # Уверенность в выборе стратегии
    "entry_price": float | None,  # Рекомендуемая цена входа
    "stop_loss": float | None,  # Рекомендуемый стоп-лосс в процентах
    "take_profit": float | None,  # Рекомендуемый тейк-профит в процентах
    "llm_usage": {
        "prompt_tokens": int,
        "completion_tokens": int,
        "total_tokens": int
    }
}
```

## Интеграция в процесс принятия решений

Метод `get_llm_guidance()` автоматически вызывается в `get_decision_with_llm()` и интегрирован в процесс анализа:

1. **Технический анализ** - базовые индикаторы
2. **Sentiment анализ** - анализ новостей
3. **LLM Guidance** - выбор стратегии на основе всех данных
4. **Детальный LLM анализ** - финальная рекомендация

Результат включает как стратегию, так и детальный анализ, что позволяет принимать более обоснованные решения.

## Примеры использования

### Пример 1: Mean Reversion стратегия

```python
result = agent.get_decision_with_llm("MSFT")

if result['llm_guidance']['strategy'] == 'Mean Reversion':
    print("Используем стратегию возврата к среднему")
    print(f"Обоснование: {result['llm_guidance']['reasoning']}")
    # Логика для Mean Reversion
```

### Пример 2: Momentum стратегия

```python
result = agent.get_decision_with_llm("SNDK")

if result['llm_guidance']['strategy'] == 'Momentum':
    print("Используем стратегию следования тренду")
    if result['llm_guidance']['entry_price']:
        print(f"Рекомендуемая цена входа: ${result['llm_guidance']['entry_price']}")
```

### Пример 3: Комбинирование с ExecutionAgent

```python
from execution_agent import ExecutionAgent

exec_agent = ExecutionAgent()
analyst = AnalystAgent(use_llm=True)

for ticker in ["MSFT", "SNDK"]:
    result = analyst.get_decision_with_llm(ticker)
    
    # Используем стратегию для принятия решения
    strategy = result['llm_guidance']['strategy']
    decision = result['decision']
    
    if decision in ['BUY', 'STRONG_BUY']:
        if strategy == 'Momentum':
            # Для Momentum стратегии можем использовать более агрессивные параметры
            exec_agent._execute_buy(ticker, decision)
        elif strategy == 'Mean Reversion':
            # Для Mean Reversion более консервативный подход
            # Можно добавить дополнительные проверки
            exec_agent._execute_buy(ticker, decision)
```

## Настройка LLM

Для работы метода требуется настройка LLM в `../brats/config.env`:

```env
OPENAI_API_KEY=your_proxyapi_key_here
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
```

## Обработка ошибок

Метод обрабатывает следующие ошибки:

1. **LLM недоступен** - возвращает стратегию "Hold" с низкой уверенностью
2. **Ошибка парсинга JSON** - пытается извлечь стратегию из текста ответа
3. **Неизвестная стратегия** - валидирует и заменяет на "Hold" при необходимости

## Логирование

Метод логирует:
- Запрос к LLM
- Выбранную стратегию и уверенность
- Обоснование (первые 100 символов)
- Ошибки при работе с LLM

## См. также

- [WEB_INTERFACE.md](../WEB_INTERFACE.md) - Использование через веб-интерфейс
- [BUSINESS_PROCESSES.md](../BUSINESS_PROCESSES.md) - Бизнес-процессы системы
- [examples/llm_guidance_example.py](../examples/llm_guidance_example.py) - Примеры использования




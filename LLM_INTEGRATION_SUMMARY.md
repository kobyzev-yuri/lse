# Сводка по интеграции LLM Guidance

## ✅ Что реализовано

### 1. Метод `get_llm_guidance()` в AnalystAgent

Метод использует LLM для выбора оптимальной торговой стратегии на основе:
- Технических данных (цена, SMA, волатильность)
- Контекста новостей (sentiment, содержание)

**Доступные стратегии:**
- **Mean Reversion** - возврат к среднему
- **Momentum** - следование тренду  
- **Hold** - удержание позиции

### 2. Интеграция в процесс анализа

Метод автоматически вызывается в `get_decision_with_llm()` и возвращает:
- Выбранную стратегию
- Обоснование выбора
- Уверенность (0.0-1.0)
- Рекомендуемые параметры (цена входа, стоп-лосс, тейк-профит)

### 3. Обновление веб-интерфейса

Веб-интерфейс теперь отображает:
- LLM стратегию с обоснованием
- Рекомендуемые параметры торговли
- Детальный LLM анализ

## Использование

### Через Python API

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=True)
result = agent.get_decision_with_llm("MSFT")

# Доступ к стратегии
strategy = result['llm_guidance']['strategy']
reasoning = result['llm_guidance']['reasoning']
confidence = result['llm_guidance']['confidence']
```

### Через веб-интерфейс

1. Откройте http://localhost:8000/trading
2. Выберите тикер
3. Включите "Использовать LLM анализ"
4. Нажмите "Анализировать"
5. Просмотрите результаты, включая выбранную стратегию

## Структура ответа

```python
{
    "decision": "BUY|STRONG_BUY|HOLD",
    "technical_signal": "BUY|HOLD",
    "sentiment": 0.75,
    "llm_guidance": {
        "strategy": "Mean Reversion" | "Momentum" | "Hold",
        "reasoning": "обоснование",
        "confidence": 0.85,
        "entry_price": 350.0,
        "stop_loss": 5.0,
        "take_profit": 10.0
    },
    "llm_analysis": {
        "decision": "STRONG_BUY",
        "confidence": 0.9,
        "reasoning": "детальный анализ",
        "risks": ["список рисков"],
        "key_factors": ["ключевые факторы"]
    }
}
```

## Файлы

- `analyst_agent.py` - метод `get_llm_guidance()` добавлен
- `templates/trading.html` - обновлен для отображения стратегии
- `docs/LLM_GUIDANCE.md` - подробная документация
- `examples/llm_guidance_example.py` - примеры использования

## Требования

Для работы метода требуется:
- `OPENAI_API_KEY` в `../brats/config.env`
- `OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1`
- LLM сервис инициализирован (`use_llm=True`)

## Следующие шаги

1. Тестирование на реальных данных
2. Настройка параметров стратегий
3. Интеграция стратегий в ExecutionAgent
4. Добавление метрик эффективности стратегий


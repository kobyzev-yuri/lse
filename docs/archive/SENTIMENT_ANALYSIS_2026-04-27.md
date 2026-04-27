# Sentiment Analysis в LSE Trading System

## Описание

Система поддерживает анализ sentiment новостей для улучшения торговых решений. Sentiment score хранится в таблице `knowledge_base` и используется в `AnalystAgent` для принятия решений.

## Текущая реализация

### Хранение sentiment

Sentiment score хранится в таблице `knowledge_base`:
- Поле: `sentiment_score DECIMAL(3,2)`
- Диапазон: 0.0 (отрицательный) до 1.0 (положительный)
- Может быть `NULL` (не рассчитан)

### Использование sentiment

1. **В AnalystAgent**:
   - Метод `calculate_weighted_sentiment()` вычисляет взвешенный sentiment
   - Новости с упоминанием тикера получают вес 2.0
   - Макро-новости получают вес 1.0
   - Используется для принятия торговых решений

2. **В ExecutionAgent**:
   - Sentiment сохраняется в `trade_history.sentiment_at_trade`
   - Используется для анализа эффективности сделок

3. **В стратегиях**:
   - Все стратегии учитывают sentiment при выборе сигнала
   - MomentumStrategy: положительный sentiment усиливает сигнал
   - MeanReversionStrategy: нейтральный sentiment предпочтителен
   - VolatileGapStrategy: экстремальный sentiment влияет на решение

## Автоматический расчет sentiment

### Настройка

В `config.env`:
```env
SENTIMENT_AUTO_CALCULATE=true
SENTIMENT_LLM_MODEL=gpt-4o
```

### Реализация

Модуль `services/sentiment_analyzer.py`:
- Использует LLM (GPT-4o через proxyapi.ru) для расчета sentiment
- Анализирует текст новости и возвращает score от 0.0 до 1.0
- Автоматически вызывается при добавлении новостей, если `SENTIMENT_AUTO_CALCULATE=true`

### Использование

#### При добавлении новостей вручную

```python
from news_importer import add_news_interactive

# Sentiment будет рассчитан автоматически, если включен в config.env
add_news_interactive()
```

#### При импорте из CSV/JSON

```python
from news_importer import import_from_csv

# Sentiment будет рассчитан для новостей без sentiment_score
import_from_csv("news.csv")
```

#### Через веб-интерфейс

При добавлении новости через веб-интерфейс sentiment рассчитывается автоматически, если включен в конфиге.

#### Прямой вызов

```python
from services.sentiment_analyzer import calculate_sentiment

content = "Microsoft announces record quarterly earnings..."
sentiment = calculate_sentiment(content)
print(f"Sentiment: {sentiment:.3f}")  # 0.0-1.0
```

## Ручной ввод sentiment

Если автоматический расчет отключен или нужно переопределить значение:

```python
from news_importer import add_news

# Указать sentiment вручную
add_news(engine, "MSFT", "Reuters", "News content...", sentiment_score=0.75)
```

## Взвешенный sentiment

Метод `calculate_weighted_sentiment()` в `AnalystAgent`:

1. **Проверяет каждую новость**:
   - Если тикер упоминается в контенте → weight = 2.0
   - Если новость для конкретного тикера → weight = 2.0
   - Макро-новости → weight = 1.0

2. **Вычисляет взвешенный средний**:
   ```
   weighted_sentiment = Σ(sentiment_score × weight) / Σ(weight)
   ```

3. **Используется для принятия решений**:
   - `weighted_sentiment > 0.5` → положительный sentiment
   - Усиливает сигналы BUY/STRONG_BUY

## Временной лаг

Sentiment учитывается с учетом временного лага:
- **Макро-события** (MACRO, US_MACRO): 72 часа
- **Обычные новости**: 24 часа

Это реализовано в методе `get_recent_news()` в `AnalystAgent`.

## Примеры

### Пример 1: Автоматический расчет при добавлении

```python
# config.env: SENTIMENT_AUTO_CALCULATE=true
from news_importer import add_news_interactive

# При вводе новости sentiment будет рассчитан автоматически
add_news_interactive()
```

### Пример 2: Ручной расчет sentiment

```python
from services.sentiment_analyzer import calculate_sentiment

news_text = "Microsoft reports strong quarterly results, stock price rises"
sentiment = calculate_sentiment(news_text)
print(f"Sentiment: {sentiment:.3f}")  # Например: 0.850
```

### Пример 3: Использование в анализе

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent()
result = agent.get_decision_with_llm("MSFT")

print(f"Sentiment: {result['sentiment']:.3f}")
print(f"Sentiment positive: {result['sentiment_positive']}")
```

## Настройка

### Включение/выключение автоматического расчета

В `config.env`:
```env
# Включить автоматический расчет
SENTIMENT_AUTO_CALCULATE=true

# Выключить (только ручной ввод)
SENTIMENT_AUTO_CALCULATE=false
```

### Настройка LLM для sentiment

В `config.env`:
```env
OPENAI_API_KEY=your_proxyapi_key_here
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
SENTIMENT_LLM_MODEL=gpt-4o  # Модель для расчета sentiment
```

## Troubleshooting

### Sentiment не рассчитывается автоматически

1. Проверьте `SENTIMENT_AUTO_CALCULATE=true` в `config.env`
2. Убедитесь, что `OPENAI_API_KEY` настроен
3. Проверьте логи на ошибки LLM

### Неточный sentiment

- LLM может давать разные результаты для одного текста
- Для более точных результатов можно использовать специализированные модели sentiment analysis
- Можно комбинировать несколько методов расчета

### Высокая стоимость LLM

- Используйте более дешевую модель для sentiment (например, `gpt-3.5-turbo`)
- Кэшируйте результаты для одинаковых текстов
- Рассчитывайте sentiment только для важных новостей

## См. также

- [config.env](config.env) - Конфигурация системы
- [config_loader.py](config_loader.py) - Загрузчик конфигурации
- [services/sentiment_analyzer.py](services/sentiment_analyzer.py) - Модуль расчета sentiment
- [analyst_agent.py](analyst_agent.py) - Использование sentiment в анализе




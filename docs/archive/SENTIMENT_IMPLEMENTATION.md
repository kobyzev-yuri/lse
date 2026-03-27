# Реализация Sentiment Analysis

## Как реализован sentiment analysis

### 1. Хранение в базе данных

Sentiment score хранится в таблице `knowledge_base`:
```sql
CREATE TABLE knowledge_base (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP,
    ticker VARCHAR(10),
    source VARCHAR(100),
    content TEXT,
    sentiment_score DECIMAL(3,2)  -- 0.0 (отрицательный) до 1.0 (положительный)
);
```

### 2. Расчет sentiment

#### Ручной ввод
При добавлении новости можно указать sentiment вручную:
```python
add_news(engine, "MSFT", "Reuters", "News content...", sentiment_score=0.75)
```

#### Автоматический расчет через LLM
Если `SENTIMENT_AUTO_CALCULATE=true` в `config.env`, sentiment рассчитывается автоматически через LLM:

```python
from services.sentiment_analyzer import calculate_sentiment

content = "Microsoft reports strong quarterly earnings..."
sentiment = calculate_sentiment(content)  # Возвращает 0.0-1.0
```

**Реализация**:
- Использует GPT-4o через proxyapi.ru
- Анализирует текст новости
- Возвращает score от 0.0 (отрицательный) до 1.0 (положительный)

### 3. Использование в анализе

#### В AnalystAgent

Метод `calculate_weighted_sentiment()`:
1. Загружает новости из `knowledge_base` для тикера
2. Присваивает веса:
   - Новости с упоминанием тикера: weight = 2.0
   - Макро-новости: weight = 1.0
3. Вычисляет взвешенный средний:
   ```
   weighted_sentiment = Σ(sentiment_score × weight) / Σ(weight)
   ```

#### В торговых стратегиях

Все стратегии учитывают sentiment:
- **MomentumStrategy**: положительный sentiment (>0.5) усиливает сигнал
- **MeanReversionStrategy**: нейтральный sentiment (0.3-0.7) предпочтителен
- **VolatileGapStrategy**: экстремальный sentiment (>0.8 или <0.2) влияет на решение

### 4. Временной лаг

Sentiment учитывается с учетом временного лага:
- **Макро-события** (MACRO, US_MACRO): 72 часа
- **Обычные новости**: 24 часа

Реализовано в `AnalystAgent.get_recent_news()`.

## Настройка

### Включение автоматического расчета

В `config.env`:
```env
SENTIMENT_AUTO_CALCULATE=true
OPENAI_API_KEY=sk-0rjJ3guVbISwIjvhypozyF4YEicN2fUY
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
```

### Отключение автоматического расчета

В `config.env`:
```env
SENTIMENT_AUTO_CALCULATE=false
```

В этом случае sentiment нужно указывать вручную при добавлении новостей.

## Примеры использования

### Пример 1: Автоматический расчет при добавлении

```python
# config.env: SENTIMENT_AUTO_CALCULATE=true
from news_importer import add_news_interactive

# Sentiment будет рассчитан автоматически
add_news_interactive()
```

### Пример 2: Импорт с автоматическим расчетом

```python
from news_importer import import_from_csv

# Для новостей без sentiment_score будет рассчитан автоматически
import_from_csv("news.csv")
```

### Пример 3: Использование в анализе

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent()
result = agent.get_decision_with_llm("MSFT")

print(f"Sentiment: {result['sentiment']:.3f}")
print(f"Sentiment positive: {result['sentiment_positive']}")
```

## Файлы

- `services/sentiment_analyzer.py` - модуль расчета sentiment через LLM
- `news_importer.py` - интеграция автоматического расчета
- `analyst_agent.py` - использование sentiment в анализе
- `config.env` - настройки (SENTIMENT_AUTO_CALCULATE)

## См. также

- [docs/SENTIMENT_ANALYSIS.md](docs/SENTIMENT_ANALYSIS.md) - Подробная документация
- [CONFIG_SETUP.md](CONFIG_SETUP.md) - Настройка конфигурации




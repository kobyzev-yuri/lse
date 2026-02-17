# Сводка: Миграция на локальный config.env

## ✅ Что сделано

### 1. Создан локальный config.env

- Файл `config.env` с настройками для LSE Trading System
- Файл `config.env.example` как шаблон
- Используются ключи из соседних проектов (brats)

### 2. Создан config_loader.py

Универсальный загрузчик конфигурации:
- Сначала ищет локальный `config.env`
- Fallback к `../brats/config.env` если локальный не найден
- Функции: `load_config()`, `get_database_url()`, `get_config_value()`

### 3. Обновлены все скрипты

Все скрипты теперь используют `config_loader.py`:
- ✅ `analyst_agent.py`
- ✅ `execution_agent.py`
- ✅ `init_db.py`
- ✅ `news_importer.py`
- ✅ `update_prices.py`
- ✅ `report_generator.py`
- ✅ `web_app.py`
- ✅ `services/llm_service.py`

### 4. Добавлен автоматический расчет sentiment

- Модуль `services/sentiment_analyzer.py` для расчета sentiment через LLM
- Автоматически вызывается при добавлении новостей (если `SENTIMENT_AUTO_CALCULATE=true`)
- Интегрирован в `news_importer.py` и веб-интерфейс

## Sentiment Analysis

### Текущая реализация

1. **Хранение**: `knowledge_base.sentiment_score` (DECIMAL(3,2), 0.0-1.0)
2. **Использование**: 
   - Взвешенный sentiment в `AnalystAgent.calculate_weighted_sentiment()`
   - Новости с упоминанием тикера: weight=2.0
   - Макро-новости: weight=1.0
3. **Автоматический расчет**: Через LLM (GPT-4o) если `SENTIMENT_AUTO_CALCULATE=true`

### Настройка

В `config.env`:
```env
SENTIMENT_AUTO_CALCULATE=true
OPENAI_API_KEY=sk-0rjJ3guVbISwIjvhypozyF4YEicN2fUY
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
```

## Использование

### Проверка конфигурации

```python
from config_loader import load_config, get_database_url

# Загрузить конфигурацию
config = load_config()
print(config)

# Получить URL БД
db_url = get_database_url()
print(db_url)
```

### Автоматический расчет sentiment

```python
# При добавлении новости sentiment рассчитывается автоматически
from news_importer import add_news_interactive
add_news_interactive()  # Sentiment будет рассчитан через LLM
```

### Ручной расчет sentiment

```python
from services.sentiment_analyzer import calculate_sentiment

content = "Microsoft reports strong earnings..."
sentiment = calculate_sentiment(content)
print(f"Sentiment: {sentiment:.3f}")
```

## Файлы

- `config.env` - локальная конфигурация (не коммитится в git)
- `config.env.example` - шаблон конфигурации
- `config_loader.py` - универсальный загрузчик
- `services/sentiment_analyzer.py` - модуль расчета sentiment
- `.gitignore` - добавлен config.env

## Миграция

Если у вас уже есть данные:
1. Скопируйте `config.env.example` в `config.env`
2. Заполните реальными значениями (можно использовать ключи из brats)
3. Все скрипты автоматически начнут использовать локальный config.env

## Преимущества

1. **Независимость** - проект не зависит от ../brats/config.env
2. **Гибкость** - можно иметь разные настройки для разных окружений
3. **Безопасность** - config.env не коммитится в git
4. **Автоматизация** - автоматический расчет sentiment через LLM

## См. также

- [CONFIG_SETUP.md](CONFIG_SETUP.md) - Подробная инструкция по настройке
- [docs/SENTIMENT_ANALYSIS.md](docs/SENTIMENT_ANALYSIS.md) - Документация по sentiment analysis




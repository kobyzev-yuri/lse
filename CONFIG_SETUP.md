# Настройка конфигурации LSE Trading System

## Создание config.env

1. Скопируйте шаблон:
```bash
cp config.env.example config.env
```

2. Отредактируйте `config.env` и заполните реальными значениями

## Ключевые параметры

### Database Configuration

```env
DATABASE_URL=postgresql://postgres:1234@localhost:5432/lse_trading
```

Используется для подключения к PostgreSQL. База данных `lse_trading` будет создана автоматически при первом запуске `init_db.py`.

### OpenAI/ProxyAPI Configuration

```env
OPENAI_API_KEY=sk-0rjJ3guVbISwIjvhypozyF4YEicN2fUY
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
```

Ключи из соседних проектов (brats, ai_reports) можно использовать напрямую.

### Sentiment Analysis

```env
SENTIMENT_AUTO_CALCULATE=true
SENTIMENT_LLM_MODEL=gpt-4o
```

Включите автоматический расчет sentiment при добавлении новостей.

### Trading Configuration

```env
INITIAL_CASH_USD=100000.0
COMMISSION_RATE=0.001
STOP_LOSS_LEVEL=0.95
```

Настройки для торговли.

## Использование ключей из других проектов

Система автоматически использует ключи из `../brats/config.env` если локальный `config.env` не найден.

Приоритет:
1. Локальный `config.env` (в директории lse)
2. `../brats/config.env` (fallback)

## Проверка конфигурации

```python
from config_loader import load_config, get_config_value

# Загрузить всю конфигурацию
config = load_config()
print(config)

# Получить конкретное значение
api_key = get_config_value('OPENAI_API_KEY')
print(f"API Key: {api_key[:10]}...")
```

## Обновление скриптов

Все скрипты обновлены для использования `config_loader.py`:
- ✅ `analyst_agent.py`
- ✅ `execution_agent.py`
- ✅ `init_db.py`
- ✅ `news_importer.py`
- ✅ `update_prices.py`
- ✅ `report_generator.py`
- ✅ `web_app.py`
- ✅ `services/llm_service.py`

## См. также

- [SENTIMENT_ANALYSIS.md](docs/SENTIMENT_ANALYSIS.md) - Настройка sentiment analysis
- [config.env.example](config.env.example) - Шаблон конфигурации


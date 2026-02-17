# Веб-интерфейс LSE Trading System

## Установка

1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Убедитесь, что в `../brats/config.env` настроен `OPENAI_API_KEY` для работы с LLM:
```env
OPENAI_API_KEY=your_proxyapi_key_here
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
```

3. Создайте необходимые директории:
```bash
mkdir -p logs templates static
```

## Запуск веб-интерфейса

```bash
python web_app.py
```

Или через uvicorn:
```bash
uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload
```

Веб-интерфейс будет доступен по адресу: http://localhost:8000

## Функциональность

### Дашборд (`/`)
- Обзор портфеля (денежные средства, открытые позиции)
- Статистика по PnL и Win Rate
- Последние сделки

### Торговля (`/trading`)
- Анализ тикеров с использованием LLM
- Исполнение торгового цикла для выбранных тикеров

### База знаний (`/knowledge`)
- Добавление новостей вручную
- Просмотр последних новостей
- Управление sentiment scores

### Визуализация (`/visualization`)
- Графики котировок с SMA
- Графики PnL по сделкам

## API Endpoints

### GET `/api/portfolio`
Получить состояние портфеля

### GET `/api/quotes/{ticker}?days=30`
Получить котировки для тикера

### POST `/api/analyze`
Анализ тикера
- `ticker`: тикер для анализа
- `use_llm`: использовать LLM (true/false)

### POST `/api/execute`
Исполнение торгового цикла
- `tickers`: список тикеров через запятую

### POST `/api/news/add`
Добавить новость
- `ticker`: тикер или MACRO/US_MACRO
- `source`: источник новости
- `content`: содержание
- `sentiment_score`: sentiment score (опционально)

### GET `/api/trades?limit=100`
Получить историю сделок

### GET `/api/pnl`
Получить PnL по закрытым сделкам

## Настройка cron задач

Для автоматического обновления данных и торговых циклов:

```bash
./setup_cron.sh
```

Это установит:
- **Обновление цен**: ежедневно в 18:00
- **Торговый цикл**: в 9:00, 13:00, 17:00 (пн-пт)

Просмотр установленных задач:
```bash
crontab -l
```

## Использование LLM анализа

AnalystAgent теперь поддерживает LLM анализ через `get_decision_with_llm()`:

```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=True)
result = agent.get_decision_with_llm("MSFT")

print(result['decision'])  # BUY, STRONG_BUY, HOLD
print(result['llm_analysis'])  # Детальный анализ от LLM
```

LLM анализ включает:
- Анализ технических индикаторов
- Анализ sentiment новостей
- Контекстный анализ ситуации
- Рекомендации с обоснованием

## Логирование

Логи сохраняются в директории `logs/`:
- `update_prices.log` - обновление цен
- `trading_cycle.log` - торговые циклы
- `cron_*.log` - логи cron задач

## Troubleshooting

### LLM не работает
- Проверьте наличие `OPENAI_API_KEY` в `../brats/config.env`
- Убедитесь, что `OPENAI_BASE_URL` указывает на `https://api.proxyapi.ru/openai/v1`

### Ошибки при запуске веб-интерфейса
- Убедитесь, что все зависимости установлены: `pip install -r requirements.txt`
- Проверьте, что PostgreSQL запущен и база данных `lse_trading` создана

### Cron задачи не выполняются
- Проверьте права на выполнение: `chmod +x scripts/*.py`
- Проверьте логи: `tail -f logs/cron_*.log`
- Проверьте путь к Python в `setup_cron.sh`




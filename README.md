# LSE Trading System

Система для торговли на Лондонской фондовой бирже с использованием PostgreSQL и pgvector.

## Требования

- Python 3.11
- PostgreSQL с расширением pgvector
- Установленные зависимости из `requirements.txt`

## Установка

1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Убедитесь, что PostgreSQL запущен и расширение pgvector установлено.

3. Скрипт автоматически использует параметры подключения из `../brats/config.env`:
   - Пароль и другие параметры берутся из файла `DATABASE_URL`
   - База данных `lse_trading` будет создана автоматически при первом запуске

## Использование

### Инициализация базы данных

Инициализация базы данных и загрузка данных:
```bash
python init_db.py
```

Скрипт создаст:
- Таблицу `quotes` для хранения котировок с метриками (SMA, волатильность)
- Таблицу `trade_kb` для базы знаний с векторными embeddings
- Таблицу `knowledge_base` для новостей с sentiment анализом
- Таблицу `portfolio_state` для состояния портфеля
- Таблицу `trade_history` для истории сделок

По умолчанию загружаются данные для тикеров: MSFT, SNDK, GBPUSD=X за последние 2 года.

**Примечание**: Формат тикеров соответствует Yahoo Finance:
- Акции: обычный формат (MSFT, SNDK)
- Валютные пары: с суффиксом `=X` (GBPUSD=X - британский фунт к доллару)
- Подробнее см. [TICKER_FORMAT.md](TICKER_FORMAT.md)

### Обновление данных

#### Обновление цен котировок

```bash
# Обновить все тикеры из БД
python update_prices.py

# Обновить конкретные тикеры
python update_prices.py MSFT,SNDK,GBPUSD=X
```

Для автоматического обновления см. [DATA_UPDATES.md](DATA_UPDATES.md)

#### Добавление новостей

```bash
# Интерактивное добавление
python news_importer.py add

# Импорт из CSV/JSON
python news_importer.py import news.csv
python news_importer.py import news.json

# Просмотр последних новостей
python news_importer.py show
```

Подробнее см. [DATA_UPDATES.md](DATA_UPDATES.md)

### Запуск торгового агента

```bash
# Запуск анализа и исполнения сделок
python execution_agent.py

# Или через Python
python -c "from execution_agent import ExecutionAgent; agent = ExecutionAgent(); agent.run_for_tickers(['MSFT', 'SNDK'])"
```

### Генерация отчетов

```bash
python report_generator.py
```

## Структура базы данных

### Таблица `quotes`
- `id` - первичный ключ
- `date` - дата котировки
- `ticker` - тикер инструмента
- `close` - цена закрытия
- `volume` - объем торгов
- `sma_5` - простая скользящая средняя за 5 дней
- `volatility_5` - волатильность за 5 дней

### Таблица `trade_kb`
- `id` - первичный ключ
- `ts` - временная метка события
- `ticker` - тикер инструмента
- `event_type` - тип события ('NEWS', 'TRADE_SIGNAL')
- `content` - текстовое содержимое
- `embedding` - векторное представление (1536 измерений для OpenAI)

### Таблица `portfolio_state`
- `id` - первичный ключ
- `ticker` - тикер инструмента или 'CASH' для баланса
- `quantity` - количество (акций или USD для CASH)
- `avg_entry_price` - средняя цена входа
- `last_updated` - время последнего обновления

### Таблица `trade_history`
- `id` - первичный ключ
- `ts` - время сделки
- `ticker` - тикер инструмента
- `side` - сторона сделки ('BUY' или 'SELL')
- `quantity` - количество акций
- `price` - цена сделки
- `commission` - комиссия
- `signal_type` - тип сигнала ('STRONG_BUY', 'STOP_LOSS', 'REBALANCE_TRIM')
- `total_value` - общая стоимость сделки
- `sentiment_at_trade` - sentiment на момент сделки

## Веб-интерфейс

Система включает веб-интерфейс для управления торговлей, базой знаний и визуализации данных.

### Запуск веб-интерфейса

```bash
python web_app.py
```

Или через uvicorn:
```bash
uvicorn web_app:app --host 0.0.0.0 --port 8000 --reload
```

Веб-интерфейс будет доступен по адресу: http://localhost:8000

Подробнее см. [WEB_INTERFACE.md](WEB_INTERFACE.md)

## Автоматизация (Cron)

Для автоматического обновления данных и торговых циклов:

```bash
./setup_cron.sh
```

Это установит:
- **Обновление цен**: ежедневно в 18:00
- **Торговый цикл**: в 9:00, 13:00, 17:00 (пн-пт)

## LLM интеграция

Система поддерживает анализ через LLM (GPT-4o через proxyapi.ru).

Для использования LLM анализа убедитесь, что в `../brats/config.env` настроены:
```env
OPENAI_API_KEY=your_proxyapi_key_here
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
```

Пример использования:
```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=True)
result = agent.get_decision_with_llm("MSFT")
print(result['decision'])  # BUY, STRONG_BUY, HOLD
print(result['llm_analysis'])  # Детальный анализ от LLM
```

## План развития

Подробный roadmap развития системы см. в файле [ROADMAP.md](ROADMAP.md)

Основные направления:
1. **Multi-Asset Rebalancing** - автоматическая ребалансировка портфеля
2. **Visual Patterns Detection** - распознавание паттернов на графиках
3. **Real-time API Integration** - переход к реальной торговле через демо-счет
4. **Векторная база знаний** - семантический поиск исторических паттернов (см. [docs/VECTOR_KB_IMPLEMENTATION.md](docs/VECTOR_KB_IMPLEMENTATION.md))

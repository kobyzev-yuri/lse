# Быстрый старт LSE Trading System

## 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

## 2. Настройка конфигурации

Убедитесь, что в `../brats/config.env` есть:
```env
DATABASE_URL=postgresql://postgres:1234@localhost:5432/brats
OPENAI_API_KEY=your_proxyapi_key_here  # Для LLM анализа
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
```

## 3. Инициализация базы данных

```bash
python init_db.py
```

Это создаст базу данных `lse_trading` и загрузит начальные данные.

## 4. Запуск веб-интерфейса

```bash
python web_app.py
```

Откройте в браузере: http://localhost:8000

## 5. Настройка автоматизации (опционально)

```bash
./setup_cron.sh
```

Это установит cron задачи для:
- Автоматического обновления цен (ежедневно в 18:00)
- Автоматического торгового цикла (9:00, 13:00, 17:00 в рабочие дни)

## Основные функции веб-интерфейса

### Дашборд
- Просмотр состояния портфеля
- Статистика по PnL и Win Rate
- Последние сделки

### Торговля
- Анализ тикеров с LLM
- Исполнение торгового цикла

### База знаний
- Добавление новостей
- Просмотр и управление новостями

### Визуализация
- Графики котировок
- Графики PnL

## Использование через Python API

### Базовый анализ
```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=False)
decision = agent.get_decision("MSFT")
print(decision)  # BUY, STRONG_BUY, HOLD
```

### Анализ с LLM
```python
from analyst_agent import AnalystAgent

agent = AnalystAgent(use_llm=True)
result = agent.get_decision_with_llm("MSFT")
print(result['decision'])
print(result['llm_analysis'])
```

### Исполнение сделок
```python
from execution_agent import ExecutionAgent

agent = ExecutionAgent()
agent.run_for_tickers(["MSFT", "SNDK"], use_llm=True)
```

## Обновление данных

### Обновление цен
```bash
python update_prices.py
```

### Добавление новостей
```bash
python news_importer.py add
```

## Генерация отчетов

```bash
python report_generator.py
```

## Troubleshooting

### Ошибка подключения к БД
- Убедитесь, что PostgreSQL запущен
- Проверьте параметры в `../brats/config.env`

### LLM не работает
- Проверьте наличие `OPENAI_API_KEY` в `../brats/config.env`
- Убедитесь, что ключ валидный для proxyapi.ru

### Веб-интерфейс не запускается
- Проверьте, что все зависимости установлены
- Убедитесь, что порт 8000 свободен

## Дополнительная документация

- [WEB_INTERFACE.md](WEB_INTERFACE.md) - Подробная документация веб-интерфейса
- [BUSINESS_PROCESSES.md](BUSINESS_PROCESSES.md) - Описание бизнес-процессов
- [ROADMAP.md](ROADMAP.md) - План развития системы
- [docs/VECTOR_KB_IMPLEMENTATION.md](docs/VECTOR_KB_IMPLEMENTATION.md) - Векторная база знаний


# Быстрые исправления после тестирования

## ✅ Что работает:
- **NewsAPI** - 6 новостей сохранено ✅
- **Alpha Vantage** - 9635 earnings + 232 новости сохранено ✅

## 🔧 Что нужно исправить:

### 1. RSS фиды - установить feedparser

```bash
pip install feedparser>=6.0.10
```

После установки запустить:
```bash
python3 services/rss_news_fetcher.py
```

### 2. Investing.com - проблема с парсингом

**Проблемы:**
- Структура HTML изменилась (таблица не находится)
- 429 Too Many Requests (слишком частые запросы)

**Исправлено:**
- ✅ Улучшен поиск таблицы (несколько селекторов)
- ✅ Увеличена задержка между запросами (5 секунд вместо 2)

**Может потребоваться:**
- Проверить актуальную структуру HTML Investing.com
- Добавить User-Agent rotation
- Использовать альтернативный источник (Trading Economics API)

## 📊 Проверка результатов в БД

```sql
-- Новости из NewsAPI
SELECT COUNT(*) FROM knowledge_base WHERE source = 'NewsAPI';

-- Earnings из Alpha Vantage
SELECT COUNT(*) FROM knowledge_base WHERE source LIKE '%Alpha Vantage%' AND event_type = 'EARNINGS';

-- Новости из Alpha Vantage
SELECT COUNT(*) FROM knowledge_base WHERE source LIKE '%Alpha Vantage%' AND event_type = 'NEWS';

-- Всего новостей по источникам
SELECT source, COUNT(*) as count 
FROM knowledge_base 
WHERE ts >= CURRENT_DATE - INTERVAL '1 day'
GROUP BY source 
ORDER BY count DESC;
```

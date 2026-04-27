# Обновление: Strategy Manager и Центрированная Шкала Sentiment

## Резюме изменений

Реализованы следующие улучшения системы:

1. **Центрированная шкала sentiment (-1.0 до 1.0)**
2. **Реорганизация стратегий в отдельные файлы**
3. **Интеллектуальный Strategy Manager**
4. **Извлечение insight из новостей**
5. **Терминологический словарь**

---

## 1. Центрированная шкала Sentiment

### Проблема
Ранее использовалась шкала 0.0-1.0, что усложняло математические операции (например, умножение сигнала на sentiment).

### Решение
Создан модуль `utils/sentiment_utils.py` с функциями:
- `normalize_sentiment(0.0-1.0) -> -1.0 до 1.0`
- `denormalize_sentiment(-1.0 до 1.0) -> 0.0-1.0)`
- `apply_sentiment_to_signal()` - применение sentiment к сигналу

### Преимущества
- Удобные математические операции (sentiment = 0 нейтрализует сигнал)
- Простое умножение: `signal * sentiment`
- Обратная совместимость (БД хранит 0.0-1.0, стратегии используют -1.0 до 1.0)

---

## 2. Реорганизация стратегий

### Структура
```
strategies/
├── __init__.py
├── base_strategy.py          # Базовый класс
├── momentum_strategy.py      # Стратегия следования тренду
├── mean_reversion_strategy.py # Стратегия возврата к среднему
└── volatile_gap_strategy.py  # Стратегия для волатильных гэпов
```

### Изменения
- Каждая стратегия в отдельном файле
- Единый интерфейс через `BaseStrategy`
- Методы `is_suitable()` и `calculate_signal()`
- Извлечение `insight` из новостей

---

## 3. Strategy Manager

### Файл: `strategy_manager.py`

Интеллектуальный диспетчер, который выбирает оптимальную стратегию на основе:
- Волатильности (volatility_ratio)
- Sentiment (центрированная шкала)
- Ценовых гэпов (gap_percent)

### Логика выбора

1. **VolatileGapStrategy**: 
   - Очень высокая волатильность (>1.5x среднего)
   - Большой гэп (>3%) или экстремальный sentiment (>0.6)

2. **MomentumStrategy**:
   - Низкая волатильность (<1.0x среднего)
   - Положительный sentiment (>0.3)

3. **MeanReversionStrategy**:
   - Высокая волатильность (>1.2x среднего)
   - Нейтральный sentiment (-0.4 до 0.4)

4. **NeutralStrategy** (fallback):
   - Используется, когда ни одна из стратегий выше не подходит
   - Режим не определён (нет тренда, гэпа, экстремального sentiment) → консервативный HOLD

### Использование

```python
from strategy_manager import get_strategy_manager

manager = get_strategy_manager()
strategy = manager.select_strategy(
    ticker="SNDK",
    technical_data={...},
    news_data=[...],
    sentiment_score=0.8  # В центрированной шкале
)
```

---

## 4. Извлечение Insight из новостей

### Обновления

1. **`services/sentiment_analyzer.py`**:
   - `calculate_sentiment()` теперь возвращает `(sentiment, insight)`
   - LLM извлекает ключевой финансовый факт (например, "рост 163%")

2. **База данных**:
   - Добавлено поле `insight TEXT` в таблицу `knowledge_base`
   - Обновлен `init_db.py` и `news_importer.py`

3. **Стратегии**:
   - Метод `_extract_insight()` в каждой стратегии
   - Insight отображается в логах и результатах

### Пример

```python
sentiment, insight = calculate_sentiment("Компания показала рост выручки на 163%")
# sentiment = 0.9
# insight = "рост 163%"
```

---

## 5. Терминологический словарь

### Файл: `docs/TRADING_GLOSSARY.md`

Содержит определения:
- **ATR** (Average True Range)
- **Sentiment Drift** (Дрейф настроений)
- **Momentum** (Следование тренду)
- **Mean Reversion** (Возврат к среднему)
- **Volatile Gap** (Волатильный гэп)
- **RSI** (Relative Strength Index)
- **Strategy Manager** (Менеджер стратегий)
- И другие термины

---

## Интеграция в AnalystAgent

### Изменения

1. **Импорты**:
   ```python
   from strategy_manager import get_strategy_manager
   from utils.sentiment_utils import normalize_sentiment, denormalize_sentiment
   ```

2. **Инициализация**:
   - `self.strategy_manager` вместо `self.strategy_factory`
   - `STRATEGY_MANAGER_AVAILABLE` вместо `STRATEGY_FACTORY_AVAILABLE`

3. **Использование**:
   - Sentiment нормализуется в центрированную шкалу для стратегий
   - Конвертируется обратно в 0.0-1.0 для LLM и совместимости
   - Результат содержит `sentiment` (0.0-1.0) и `sentiment_normalized` (-1.0 до 1.0)

---

## Миграция данных

### База данных
- Поле `insight` добавлено в `knowledge_base`
- Существующие записи имеют `insight = NULL`
- Новые записи автоматически получают insight при расчете sentiment

### Код
- Старый `strategies.py` можно удалить (но оставлен для обратной совместимости)
- Все импорты обновлены на новые модули

---

## Тестирование

### Проверка утилит sentiment
```bash
python -c "from utils.sentiment_utils import normalize_sentiment; print(normalize_sentiment(0.5))"
# Вывод: 0.0 (нейтральный)
```

### Проверка Strategy Manager
```bash
python -c "from strategy_manager import get_strategy_manager; sm = get_strategy_manager(); print(len(sm.get_all_strategies()))"
# Вывод: 3
```

---

## Следующие шаги

1. ✅ Центрированная шкала sentiment
2. ✅ Реорганизация стратегий
3. ✅ Strategy Manager
4. ✅ Извлечение insight
5. ✅ Терминологический словарь
6. 🔄 Обновление веб-интерфейса для отображения insight
7. 🔄 Добавление новых стратегий (при необходимости)

---

## См. также

- [Portfolio Game](docs/PORTFOLIO_GAME.md)
- [Trading Glossary](docs/TRADING_GLOSSARY.md)
- [Strategies, LLM and Reports](docs/STRATEGIES_LLM_AND_REPORTS.md)




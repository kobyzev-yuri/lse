# Исправление установки feedparser

## Проблема
Ошибка при установке `feedparser` из-за устаревшей зависимости `sgmllib3k`.

## Рекомендуемое решение: Python 3.11 + conda env py11

```bash
conda activate py11
pip install feedparser
# или
pip install -r requirements.txt
```

Скрипт `test_all_news_sources.sh` автоматически активирует `py11` перед запуском.

---

## Альтернативные решения

### Вариант 1: Установить без зависимостей (если остаётесь на py310)
```bash
pip install feedparser --no-deps
pip install sgmllib3k --no-deps || true  # Игнорируем ошибку если не установится
```

### Вариант 2: Установить через conda (если используете conda)
```bash
conda install -c conda-forge feedparser
```

### Вариант 3: Установить wheel отдельно
```bash
pip install wheel
pip install feedparser --no-build-isolation
```

### Вариант 4: Использовать более новую версию (может не требовать sgmllib3k)
```bash
pip install feedparser==6.0.11 --no-deps
```

### Вариант 5: Установить все зависимости из requirements.txt
```bash
pip install -r requirements.txt
```

## Проверка установки
```bash
python3 -c "import feedparser; print('feedparser OK, version:', feedparser.__version__)"
```

## Если ничего не помогает
Можно временно использовать альтернативу - парсить RSS вручную через `requests` и `xml.etree.ElementTree`.

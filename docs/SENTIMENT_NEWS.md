# Оценка sentiment новостей

Кто считает sentiment для новостей и как перейти на бесплатную модель.

---

## Кто сейчас считает sentiment

Оценку настроения (sentiment) по тексту новости делают в **`services/sentiment_analyzer.calculate_sentiment(content)`**. Используется в:

- крон **add_sentiment_to_news_cron** — проставляет sentiment новостям из RSS/NewsAPI без оценки;
- **news_importer** (CLI/CSV/веб) — при добавлении новости, если не передан готовый sentiment;
- **web_app** — при ручном добавлении новости через форму.

По умолчанию sentiment считается через **платный LLM** (тот же, что в config: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, например gpt-4o через proxyapi). LLM возвращает число 0–1 и краткий **insight** (ключевой факт из новости).

---

## Переход на бесплатную модель (transformers)

В конфиге можно включить расчёт sentiment **без LLM** — одной из бесплатных моделей HuggingFace.

1. В **config.env** задать:
   ```env
   SENTIMENT_METHOD=transformers
   ```
   Опционально указать модель (по умолчанию FinBERT для финансовых текстов):
   ```env
   SENTIMENT_MODEL=ProsusAI/finbert
   ```

2. Зависимости уже есть в проекте: **transformers** (и при необходимости **torch**) используются для эмбеддингов и т.п. Для FinBERT достаточно `pip install transformers torch`.

3. При первом вызове модель скачается с HuggingFace (один раз). Дальше считается локально, без API и без затрат.

**Поведение:**

- **SENTIMENT_METHOD=llm** (по умолчанию) — платный LLM, возвращается (sentiment 0–1, insight).
- **SENTIMENT_METHOD=transformers** — локальная модель; возвращается (sentiment 0–1, **insight=None**). Крон add_sentiment_to_news работает и при **USE_LLM=false**, если задан **SENTIMENT_METHOD=transformers**.

При переходе на **LLM-вход** (GAME_5M_ENTRY_STRATEGY=llm) инсайт по новостям будет формироваться уже в решении LLM — отдельный insight из sentiment-модуля не обязателен. Пока используем технический вход, достаточно только sentiment (transformers без insight).

**Примеры моделей:**

| Модель | Описание |
|--------|----------|
| **ProsusAI/finbert** | Финансовые тексты (positive/negative/neutral), хорошо подходит для новостей по рынку. |
| cardiffnlp/twitter-roberta-base-sentiment-latest | Общий sentiment по коротким текстам. |
| distilbert-base-uncased-finetuned-sst-2-english | Классический sentiment (positive/negative), быстрая. |

Задать свою модель: **SENTIMENT_MODEL=имя_модели_на_HuggingFace**.

---

## Как протестировать (transformers + FinBERT)

1. В **config.env** выставить:
   ```env
   SENTIMENT_METHOD=transformers
   SENTIMENT_MODEL=ProsusAI/finbert
   ```
2. Запустить тестовый скрипт:
   ```bash
   python scripts/test_sentiment_transformers.py
   ```
   Скрипт выведет sentiment по трём примерам (позитив / негатив / нейтраль). При первом запуске модель скачается с HuggingFace. Insight при transformers не показывается (—).

---

## Итог

- Сейчас sentiment по умолчанию считает **тот же LLM, что и остальной контур** (gpt-4o и т.д.) через `llm_service.generate_response`.
- Чтобы не тратить лимит API на новости, задайте **SENTIMENT_METHOD=transformers** и при необходимости **SENTIMENT_MODEL=ProsusAI/finbert** — оценка будет бесплатной и локальной, без insight.

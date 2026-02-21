# T4 GPU и интеграция с Gemini (эмбеддинги и агент)

Кратко: **T4 для эмбеддингов не обязателен**. Удобно сделать связку агента с **Gemini** (токены, один провайдер): эмбеддинги и чат через Gemini API.

---

## 1. Нужен ли T4 для эмбеддингов?

**Нет.** Сейчас эмбеддинги считаются так:

- **Модель:** `sentence-transformers/all-mpnet-base-v2` (768 измерений).
- **Где:** локально на CPU в `services/vector_kb.py` (ленивая загрузка при первом вызове).
- **Когда:** при backfill embedding (`sync_vector_kb_cron.py`) и при запросах бота `/ask` (поиск по `knowledge_base`).

На VM **e2-small / e2-medium** этого достаточно: модель грузится один раз, запросы не массовые. T4 имеет смысл только если вы будете гонять **очень большие батчи** эмбеддингов (десятки тысяч текстов в день) и захотите ускорить — тогда отдельная VM с T4 или Cloud Run с GPU. Для текущей нагрузки **GPU не нужен**.

---

## 2. Удобство: один провайдер — Gemini (эмбеддинги + агент)

Имеет смысл связать агента и эмбеддинги с **Gemini API**:

- Один ключ и одна биллинговая сущность (Gemini tokens).
- Эмбеддинги через [Gemini Embedding API](https://ai.google.dev/gemini-api/docs/embeddings): модель поддерживает **outputDimensionality** — можно выставить **768**, чтобы совпало с колонкой `knowledge_base.embedding` (vector(768)).
- Чат/агент — через Gemini (generateContent) вместо или вместе с proxyapi.ru/OpenAI.

Тогда на сервере не обязательно тянуть sentence-transformers и можно не думать о T4: эмбеддинги идут в облако, агент тоже в Gemini.

---

## 3. Gemini: эмбеддинги

- Модель: например `text-embedding-004` / `gemini-embedding-001`.
- Размерность: по умолчанию 3072; через параметр **outputDimensionality** можно задать **768** (как в `knowledge_base.embedding`) или 1536.
- Лимиты и квоты: см. [документацию Gemini](https://ai.google.dev/gemini-api/docs).

Реализация: в `VectorKB` добавить опцию провайдера — либо локальный sentence-transformers (как сейчас), либо вызов Gemini Embedding API; при выборе Gemini размерность брать 768 и писать в существующую колонку `embedding vector(768)`.

---

## 4. Gemini: агент (LLM)

Сейчас агент ходит в **OpenAI-совместимый API** (proxyapi.ru, GPT-4o) через `services/llm_service.py`. Чтобы «сразу сделать связь агента с Gemini»:

- Добавить в конфиг выбор бэкенда: `LLM_BACKEND=openai` | `LLM_BACKEND=gemini`.
- При `LLM_BACKEND=gemini` использовать **Google Generative AI** (Gemini) для:
  - `generate_response` (чат для бота и аналитики),
  - при необходимости `analyze_trading_situation` и других вызовов агента.

Тогда один токен/биллинг Gemini можно использовать и для эмбеддингов, и для агента.

---

## 5. Что сделать по шагам (рекомендация)

1. **T4:** не подключать для текущей нагрузки; оставить эмбеддинги на CPU (sentence-transformers) или перевести на Gemini (см. ниже).
2. **Эмбеддинги через Gemini (опционально):**
   - В `config.env`: `GEMINI_API_KEY=...`, флаг типа `USE_GEMINI_EMBEDDINGS=true`.
   - В `VectorKB`: если флаг включён — вызывать Gemini Embedding API с `outputDimensionality=768`, иначе — sentence-transformers как сейчас.
3. **Агент через Gemini:**
   - В `config.env`: `LLM_BACKEND=gemini`, `GEMINI_API_KEY=...`.
   - В `llm_service.py`: при `LLM_BACKEND=gemini` использовать `google-generativeai` для генерации ответов (и при необходимости для анализа ситуации), сохраняя текущий интерфейс `generate_response` / `analyze_trading_situation`.

После этого связка «эмбеддинги + агент» через Gemini будет в одном месте, с единым учётом токенов; T4 при такой схеме не нужен.

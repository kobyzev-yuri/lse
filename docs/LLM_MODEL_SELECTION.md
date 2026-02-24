# Подбор LLM для агента: финансовое прогнозирование и сравнение моделей

Задача: выбрать адекватную модель для агента (игра 5m, новости, рекомендации) и сравнивать прогнозы разных LLM. Ниже — выводы из исследований, рекомендуемые модели и настройка через [ProxyAPI](https://proxyapi.ru/docs/overview).

---

## 0. ProxyAPI: официальная документация

По [документации ProxyAPI](https://proxyapi.ru/docs/overview):

- **Что такое ProxyAPI:** универсальный доступ к API OpenAI, Anthropic, Google и др. Запросы идут на `https://api.proxyapi.ru`, проксируются в Европу, ответ возвращается вам. Отдельные аккаунты у провайдеров не нужны — ключ и оплата только в [личном кабинете ProxyAPI](https://console.proxyapi.ru/).
- **Ключ API:** получают в разделе [Ключи API](https://console.proxyapi.ru/keys) после регистрации. Ключ целиком показывается **один раз** при создании.
- **Авторизация:** заголовок `Authorization: Bearer <КЛЮЧ>`. Для совместимости с библиотеками принимаются также `x-api-key`, параметр `key` в запросе и т.д. — везде используется **ключ ProxyAPI**.
- **Базовый путь:** `https://api.proxyapi.ru` + путь провайдера, например:
  - OpenAI: `https://api.proxyapi.ru/openai/v1`
  - OpenRouter: `https://api.proxyapi.ru/openrouter/v1` (см. [OpenRouter через ProxyAPI](https://proxyapi.ru/docs/openrouter)).

**Важно про OpenRouter в ProxyAPI:** модели **OpenAI, Anthropic и Google** в ProxyAPI поддерживаются **отдельно** (свои эндпоинты), и **не доступны через OpenRouter**. Через `https://api.proxyapi.ru/openrouter/v1` доступны остальные модели из [списка OpenRouter](https://openrouter.ai/models) (Mistral, Qwen, MiniMax, GLM и т.д.). Для GPT-4, Claude, Gemini используем соответствующие разделы документации ProxyAPI (OpenAI, Anthropic, Google), а не OpenRouter.

---

## 1. Что говорят исследования (финансовое прогнозирование)

- **GPT-4 (4o, 4-Turbo)** — сильные результаты в бенчмарках по предсказанию акций и количественным задачам; хорошо работают структурированные промпты (scratchpad, пошаговые рассуждения).
- **Claude (3.5 Sonnet, Opus)** — сопоставимы с GPT-4 в финансовых и количественных тестах; в Market-Bench / QuantEval фигурируют как топовые.
- **Gemini (1.5 Pro, 2.5 Pro, 3.x)** — в описаниях OpenRouter явно упоминаются «financial modeling», «spreadsheet-based workflows»; подходят для агентных и структурированных задач.
- **Mistral** — в работах по fine-tuning на финансовых новостях показывает устойчивую эффективность для предсказания доходностей по новостям.
- **Специализированные модели** (StockGPT, TimeGPT и т.д.) — дают выигрыш в чисто временных рядах, но в нашем сценарии агент опирается на текст (новости, KB, рассуждения), поэтому универсальные LLM с хорошим reasoning остаются основным выбором.

**Практический вывод:** для агента разумно тестировать несколько моделей с сильным reasoning и, по возможности, с отмеченной «финансовой» ориентацией: GPT-4o, Claude Sonnet/Opus, Gemini Pro, при необходимости — Mistral и открытые модели (Qwen, GLM и т.д.).

---

## 2. Какие модели через ProxyAPI и как их выбирать

### GPT-4, Claude, Gemini — через «родные» эндпоинты ProxyAPI

Для моделей **OpenAI, Anthropic, Google** в ProxyAPI используются отдельные разделы документации и свои базовые URL (не OpenRouter):

| Провайдер | Базовый URL (ProxyAPI) | Модель в конфиге | Пример |
|-----------|------------------------|-------------------|--------|
| OpenAI | `https://api.proxyapi.ru/openai/v1` | имя модели OpenAI | `gpt-4o`, `gpt-4-turbo` |
| Anthropic | см. [документацию Anthropic](https://proxyapi.ru/docs) | — | Claude Sonnet, Opus |
| Google | см. [документацию Google](https://proxyapi.ru/docs) | — | Gemini Pro, Flash |

В нашем проекте сейчас используется только OpenAI-совместимый клиент и `OPENAI_BASE_URL` + `OPENAI_MODEL`, поэтому для **сравнения моделей OpenAI** (gpt-4o, gpt-4-turbo и т.д.) достаточно менять `OPENAI_MODEL`, оставляя `OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1`. Для Claude/Gemini потребуется при необходимости отдельная интеграция по документации ProxyAPI.

### Остальные модели — через OpenRouter в ProxyAPI

По [OpenRouter через ProxyAPI](https://proxyapi.ru/docs/openrouter): базовый URL **`https://api.proxyapi.ru/openrouter/v1`**. Доступны любые модели из [списка OpenRouter](https://openrouter.ai/models), **кроме** моделей OpenAI, Anthropic и Google (они у ProxyAPI в отдельных эндпоинтах). Один и тот же **ключ ProxyAPI** используется и для OpenAI, и для OpenRouter.

Для сравнения с gpt-4o удобно подключать через OpenRouter, например:

| Модель (id для OPENAI_MODEL при base openrouter/v1) | Зачем пробовать |
|-----------------------------------------------------|------------------|
| `mistralai/mistral-medium-3.1` | Пример из документации ProxyAPI; Mistral силён по новостям в исследованиях. |
| `mistralai/mistral-large` | Более мощный Mistral. |
| `minimax/minimax-m2.5` | В OpenRouter отмечен в категории Finance. |
| `z-ai/glm-5` | В OpenRouter отмечен Finance, открытая модель. |
| `qwen/qwen3-max-thinking` | Многошаговые рассуждения. |
| `qwen/qwen3.5-plus-02-15` | Баланс качества и стоимости. |

Стоимость: OpenRouter возвращает в ответе `usage.cost` (USD); ProxyAPI пересчитывает в рубли и добавляет комиссию (подробнее в [документации OpenRouter ProxyAPI](https://proxyapi.ru/docs/openrouter)).

---

## 3. Настройка в проекте (config.env)

Текущий LLM читает `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY` из конфига (см. `services/llm_service.py`). Везде используется **один ключ ProxyAPI** ([получить в кабинете](https://console.proxyapi.ru/keys)).

### Вариант A: модели OpenAI (как сейчас)

```env
OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_MODEL=gpt-4o
OPENAI_API_KEY=<ключ ProxyAPI>
```

Для сравнения моделей OpenAI меняйте только `OPENAI_MODEL` (например `gpt-4-turbo`).

### Вариант B: модели OpenRouter (Mistral, Qwen, MiniMax и др.)

По [документации](https://proxyapi.ru/docs/openrouter) подставляем базовый путь OpenRouter и id модели из [openrouter.ai/models](https://openrouter.ai/models):

```env
OPENAI_BASE_URL=https://api.proxyapi.ru/openrouter/v1
OPENAI_MODEL=mistralai/mistral-medium-3.1
OPENAI_API_KEY=<тот же ключ ProxyAPI>
```

Проверка: запрос на `POST https://api.proxyapi.ru/openrouter/v1/chat/completions` с `Authorization: Bearer <КЛЮЧ>` и телом как в [примере из документации](https://proxyapi.ru/docs/openrouter).

---

## 4. Сравнение прогнозов разных LLM (LLM_COMPARE_MODELS)

Включено сравнение нескольких моделей в одном прогоне. В `config.env` задаётся список моделей:

```env
# Через запятую: "model" (тот же base_url что OPENAI_BASE_URL) или "provider|model"
# provider = openai | anthropic | google (базовые URL ProxyAPI)
LLM_COMPARE_MODELS=gpt-4o,openai|gpt-5.2,anthropic|claude-opus-4-6,google|gemini-3.1-pro-preview
```

При включённом `USE_LLM_NEWS` и при вызове LLM для новостей по тикеру (в т.ч. в `get_decision_5m`) один и тот же запрос отправляется во все перечисленные модели. Результат основной модели (из `OPENAI_MODEL`) по‑прежнему попадает в `llm_news_content`, `llm_sentiment`, `llm_insight`; ответы всех моделей дополнительно возвращаются в **`llm_comparison`**: список `{ "model", "content", "sentiment_score", "insight" }` (или `"error"` при сбое). Так можно сравнивать gpt-4o, gpt-5.2, claude-opus-4-6, gemini-3.1-pro-preview в одном запросе.

Базовые URL по провайдерам (в коде): `openai` → `https://api.proxyapi.ru/openai/v1`, `anthropic` → `https://api.proxyapi.ru/anthropic/v1`, `google` → `https://api.proxyapi.ru/google/v1`. При необходимости уточните пути в [документации ProxyAPI](https://proxyapi.ru/docs) для Anthropic и Google.

- Ручное A/B: менять только `OPENAI_MODEL` и перезапускать сервисы.
- Фиксация в БД: при сохранении решения можно дописать в источник имя модели; при наличии `llm_comparison` — сохранять его в отдельное поле или таблицу для последующего разбора.

---

## 5. Краткий чеклист подбора модели

- [ ] Иметь один ключ ProxyAPI ([Ключи API](https://console.proxyapi.ru/keys)); для OpenRouter используется тот же ключ.
- [ ] Для OpenAI: `OPENAI_BASE_URL=https://api.proxyapi.ru/openai/v1`, менять только `OPENAI_MODEL` (gpt-4o, gpt-4-turbo и т.д.).
- [ ] Для сравнения с другими провайдерами: `OPENAI_BASE_URL=https://api.proxyapi.ru/openrouter/v1`, `OPENAI_MODEL` = id из [OpenRouter](https://openrouter.ai/models) (например `mistralai/mistral-medium-3.1`, `minimax/minimax-m2.5`).
- [ ] Запускать игру 5m / сбор новостей с разными моделями, сохранять вывод с указанием модели.
- [ ] Оценить: стабильность сигналов, качество рассуждений, латентность, стоимость; зафиксировать модель для продакшена.

---

## См. также

- [ProxyAPI — начало работы](https://proxyapi.ru/docs/overview), [OpenRouter через ProxyAPI](https://proxyapi.ru/docs/openrouter).
- `services/llm_service.py` — инициализация клиента по `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`.
- `config.env.example` — шаблон конфига.
- [OpenRouter Models](https://openrouter.ai/models) — список моделей для эндпоинта OpenRouter.
- [GAME_SNDK.md](GAME_SNDK.md) — использование LLM в решении 5m (новости, KB, сессия биржи).

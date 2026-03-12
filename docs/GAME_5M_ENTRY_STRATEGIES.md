# Две стратегии входа GAME_5M

Краткая сводка: какой флаг что выбирает, значение по умолчанию и чем отличаются сохраняемые параметры сделки.

---

## Флаг и значение по умолчанию

| Параметр | Значение по умолчанию | Варианты |
|----------|------------------------|----------|
| **`GAME_5M_ENTRY_STRATEGY`** | `technical` | `technical` \| `llm` |

- **Не задан или пусто** → считается `technical`.
- Задаётся в `config.env` (или переменных окружения). Пример в `config.env.example`: закомментирована строка `# GAME_5M_ENTRY_STRATEGY=llm`.

---

## Что выбирает каждая стратегия

| Стратегия | Описание |
|-----------|----------|
| **technical** | Решение о входе только по правилам 5m: RSI, импульс 2ч, волатильность, новости KB, фаза сессии. Без LLM и без учёта корреляций/кластера. |
| **llm** | Те же данные плюс контекст корреляций и связанных тикеров. Решение принимает LLM (BUY/STRONG_BUY/HOLD/SELL). Вход записывается только если LLM вернул BUY или STRONG_BUY. |

При `llm` крон перед записью входа вызывает `llm.analyze_trading_situation` с `cluster_note`; при недоступности LLM или отсутствии кластерного контекста используется техническое решение.

---

## Сохраняемые параметры по сделке (context_json)

Оба типа входа пишут в `trade_history.context_json` один и тот же **базовый** снимок через `services.deal_params_5m.build_full_entry_context(d5)`:

- `deal_params_version`, `entry_impulse_pct` (= momentum_2h_pct), `decision`, `reasoning`, `price`;
- RSI, волатильность, session_high, period_str, стоп/тейк %, high_5d/low_5d, бары, kb_news_impact и т.д. (см. `FULL_ENTRY_KEYS` в `deal_params_5m.py`).

**Отличия по стратегии:**

| Поле | technical | llm |
|------|-----------|-----|
| **`entry_strategy`** | `"technical"` | `"llm"` |
| **`decision`**, **`reasoning`** | От `get_decision_5m()` (правила + KB + сессия) | От LLM (с учётом корреляций) |
| **`llm_key_factors`** | Нет (не пишется) | Список ключевых факторов из ответа LLM |

Единая схема позволяет в отчётах (/closed, /closed_impulse) и в аналитике фильтровать и сравнивать сделки по `entry_strategy` без разбора двух разных форматов.

Подробнее: `docs/GAME_5M_BUY_DECISION_AND_LLM.md`, `docs/GAME_5M_DEAL_PARAMS_JSON.md`.

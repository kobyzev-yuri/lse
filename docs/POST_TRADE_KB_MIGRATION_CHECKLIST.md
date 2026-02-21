# Чеклист после перехода с trade_kb на одну таблицу knowledge_base

## 1. Кто и когда запускает init_db

**init_db никто не запускает автоматически.** Его запускает человек (или скрипт развёртывания) вручную:

- **Команда:** `python init_db.py`
- **Когда:**
  - Первая настройка проекта: создаётся БД `lse_trading`, таблицы (quotes, knowledge_base, portfolio_state, trade_history), расширение pgvector, колонки в knowledge_base в т.ч. **embedding** и **outcome_json**.
  - После обновления кода, когда в init_db добавлены новые таблицы/колонки — чтобы подтянуть схему на существующую БД.
- **Cron init_db не вызывает.** Индекс по вектору (kb_embedding_idx) создаётся в init_db при наличии ≥10 записей с embedding; при необходимости его можно создать вручную.

Итог: **init_db = ручной запуск** при установке или при изменении схемы.

---

## 2. Код после отказа от trade_kb (проверено)

| Компонент | Статус |
|-----------|--------|
| `init_db.py` | Таблица trade_kb не создаётся; в knowledge_base добавлены embedding, outcome_json, индекс kb_embedding_idx |
| `services/vector_kb.py` | Пишет/читает только knowledge_base; sync_from_knowledge_base = backfill embedding |
| `services/news_impact_analyzer.py` | Читает/пишет ts, outcome_json в knowledge_base |
| `scripts/analyze_event_outcomes_cron.py` | Выборка из knowledge_base |
| `analyst_agent.py` (get_recent_news) | Только knowledge_base, объединение с trade_kb убрано |
| `services/telegram_bot.py` | Подпись «Векторная БЗ» / source trade_kb убрана |
| `scripts/sync_vector_kb_cron.py` | Логи и статистика по knowledge_base, backfill embedding |
| `scripts/migrate_trade_kb_to_knowledge_base.py` | Единственное место, где упоминается trade_kb — одноразовая миграция (читает из trade_kb и удаляет её) |

Код приведён к одной таблице knowledge_base.

---

## 3. Документация: что обновлено и что поправить

### Уже приведены к одной таблице

- `docs/NEWS_TABLES_KNOWLEDGE_BASE_VS_TRADE_KB.md` — описание одной таблицы, миграция, backfill
- `docs/KNOWLEDGE_BASE_SINGLE_TABLE.md` — решение и реализация

### Обновлено (упоминания trade_kb → knowledge_base)

- **README.md** — раздел «Структура БД»: убрана trade_kb, указана knowledge_base (embedding, outcome_json)
- **docs/DEPLOY_INSTRUCTIONS.md** — список таблиц обновлён
- **docs/LOW_COST_GCP_SERVER.md** — список таблиц обновлён
- **docs/TELEGRAM_BOT_SETUP.md** — БД: одна таблица knowledge_base
- **BUSINESS_PROCESSES.md** — диаграммы и текст переведены на knowledge_base
- **docs/VECTOR_KB_USAGE.md** — описание таблицы заменено на knowledge_base и колонки embedding/outcome_json

### Доки с упоминаниями trade_kb (исторический контекст или детали)

- `docs/GEMINI_AND_EMBEDDINGS.md` — заменить «поиск по trade_kb» на knowledge_base
- `docs/TRADING_AGENT_READINESS.md` — trade_kb → knowledge_base
- `docs/QUICK_START_NEWS_ANALYSIS.md` — trade_kb → knowledge_base
- `docs/NEWS_ANALYSIS_AUTOMATION.md` — trade_kb → knowledge_base
- `docs/BOSS_DASHBOARD_NEWS_IMPACT_SUMMARY.md` — outcome_json теперь в knowledge_base
- `docs/NEWS_IMPACT_TRACKING.md` — поиск в knowledge_base
- `docs/BOSS_DASHBOARD_VECTOR_KB_INTEGRATION.md` — поиск в knowledge_base
- `docs/NEWS_IMPACT_INTEGRATION_PLAN.md`, `docs/VECTOR_KB_IMPLEMENTATION.md` — планы/история; при желании добавить в начало примечание «архитектура: одна таблица knowledge_base, см. KNOWLEDGE_BASE_SINGLE_TABLE.md»

---

## 4. Порядок действий по шагам (без ошибок)

1. **Уже сделано:** миграция данных (`migrate_trade_kb_to_knowledge_base.py`), backfill embedding (`sync_vector_kb_cron.py`), код переведён на knowledge_base.
2. **Один раз после деплоя / новой БД:** запустить `python init_db.py` (создаёт схему и при необходимости индекс kb_embedding_idx). На уже мигрированной БД init_db можно не запускать, если схема уже с embedding/outcome_json.
3. **Cron:** оставить задачу, которая вызывает `sync_vector_kb_cron.py` (backfill embedding в knowledge_base). Задачи «запуск init_db» в cron нет и не нужна.
4. **Документация:** пройти список из п. 3 и заменить/уточнить упоминания trade_kb в перечисленных файлах.

После этого схема «одна таблица knowledge_base» и описание в доках согласованы.

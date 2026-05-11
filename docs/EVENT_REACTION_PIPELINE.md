# Event / earnings: feature builder, outcomes и регулярный контроль

Состояние репозитория: **`event_reaction_dataset`** заполняется скелетом из KB (`scripts/build_event_reaction_dataset.py`); колонки **`features_before` / `outcomes_after` / `final_label`** и вспомогательные таблицы пока не ведутся кодом. Ниже — как это **реализовать по шагам**, **гонять регулярно** и **контролировать**.

Дизайн-источник: [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md) §4.2.1.

---

## Принципы (чтобы было контролируемо)

1. **Идемпотентность** — каждый джоб делает `UPDATE … WHERE условие «ещё не заполнено»` или `ON CONFLICT DO UPDATE` для вспомогательных таблиц. Повторный запуск не ломает данные.
2. **Версии**  
   - **`dataset_version`** (`v0`, `v1`…) — смена при изменении **состава строк** или уникального ключа.  
   - **`feature_builder_version`** (строка внутри JSON `features_before`) — смена при изменении **формулы признаков**.  
   - Аналогично **`outcome_builder_version`** в `outcomes_after`.
3. **Батчи** — флаги `--limit N`, `--since`, `--until`, `--id-from` / `--id-to`, чтобы не блокировать БД и дебажить на малом объёме.
4. **Dry-run** — `--dry-run` печатает счётчики и примеры без `COMMIT`.
5. **Логи** — append в `/app/logs/ml/...` или общий `logs/` на хосте; в логе: версия билдера, N обработанных, N пропусков (нет котировок), ошибки по тикеру.
6. **Контроль качества** — уже есть: `GET /api/ml/data-quality` → `event_analytics` (доли `with_features_before`, `with_outcomes_after`, `labeled`). Целевые пороги зафиксировать у себя (например: через квартал >50% строк с features для `v0`).

---

## Пункт 3 — вспомогательные таблицы (по желанию до или параллельно с X/y)

### `market_regime_daily`

- **Назначение:** один ряд на **торговую дату** US: индексы, VIX, агрегаты в `regime_flags` / `features_json`.
- **Реализация:** отдельный скрипт `scripts/ingest_market_regime_daily.py` (новый): читает **daily** цены из вашей таблицы `quotes` и/или внешний источник для `SPY`/`QQQ`/`DIA`/`^VIX` (как у вас принято в проекте), UPSERT по `trade_date`.
- **Регулярность:** **раз в торговый день** после обновления daily-котировок (например сразу после `ingest_market_bars_intraday` или отдельный cron 00:30 MSK).

### `peer_graph_edge`

- **Назначение:** рёбра «тикер → аналог / сектор» для peer-features.
- **Реализация:**  
  - MVP: статический импорт из конфига (`TICKERS_FAST`, кластеры портфеля, секторные списки) → `INSERT … ON CONFLICT DO UPDATE`.  
  - Позже: пересчёт весов по корреляциям rolling window (offline-джоб раз в неделю).
- **Регулярность:** редко (раз в неделю или при смене universe).

### `earnings_event_detail`

- **Назначение:** EPS/revenue actual vs estimate, привязка к `knowledge_base_id`.
- **Реализация:** отдельный импорт из **поставщика** (yfinance, FMP, и т.д.) по `(ticker, fiscal_period)` или по `kb.id`; не смешивать с «скелетом» `event_reaction_dataset`.
- **Регулярность:** после отчётов (раз в квартал на тикер) + ручной догон для истории.

---

## Пункт 1 — `features_before`

- **Вход:** строки `event_reaction_dataset` с `features_before = '{}'` (и при необходимости фильтр `dataset_version`).
- **Логика (MVP → расширение):**  
  1. **Календарь:** `event_time_et` → якорная **торговая дата** `as_of` (последний close **не позже** события, по правилам leak-safe из дизайна).  
  2. Из **`quotes`** (daily): log-returns 5d/20d до `as_of`, волатильность, расстояние от high20, объём z — всё уже в духе `portfolio_ml_features`.  
  3. Опционально: снимок из **`market_regime_daily`** на `as_of`, peer-агрегаты по **`peer_graph_edge`**.  
  4. Сериализовать в один JSON, добавить `feature_builder_version`, `as_of_trade_date`, `built_at_utc`.
- **Код:** новый модуль `services/event_reaction_features.py` + CLI `scripts/backfill_event_reaction_features.py` с `--limit`, `--dataset-version`, `--dry-run`.
- **Регулярность:**  
  - **Инкрементально:** cron **ежедневно** — обработать не более N новых/пустых строк (например 500).  
  - **Полный пересчёт** при смене `feature_builder_version`: отдельный запуск с другим фильтром или новым `dataset_version`.

---

## Пункт 2 — `outcomes_after` и `final_label`

- **Когда:** только если от `event_time_et` прошло **достаточно календарных/торговых дней** (горизонты 1d / 5d / 20d — как в дизайне).
- **Логика:** по тем же **daily** `quotes`: forward log-returns от якорной цены (close T0 или open T+1 — зафиксировать в доке и не менять без новой версии), max drawdown в окне, опционально объём.
- **`final_label`:** rule-based из `outcomes_after` (например UP/DOWN/FLAT по порогу в log-space с учётом издержек — аналогично `PORTFOLIO_ML_*_BPS` в портфельной модели).
- **Код:** `services/event_reaction_outcomes.py` + `scripts/backfill_event_reaction_outcomes.py` с `--min-age-days`, `--horizons 1,5,20`.
- **Регулярность:** cron **ежедневно** (ночь US): обновлять только строки, у которых «событие + max горизонт» уже в прошлом и `outcomes_after` пустой.

---

## Регулярность и cron (пример)

Порядок зависимостей: **котировки** → (опционально) **`market_regime_daily`** → **features** → спустя время → **outcomes**.

Пример строк (хост, `docker exec lse-bot`, логи на volume):

```text
# Режим рынка (если скрипт добавлен)
15 1 * * 1-5  flock -n /tmp/lse_market_regime.lock docker exec lse-bot python scripts/ingest_market_regime_daily.py >> ~/lse/logs/event_regime.log 2>&1

# Признаки: батч пустых строк
30 2 * * *     flock -n /tmp/lse_erd_features.lock docker exec lse-bot python scripts/backfill_event_reaction_features.py --limit 2000 >> ~/lse/logs/event_reaction_features.log 2>&1

# Исходы: только созревшие по дате
45 2 * * *     flock -n /tmp/lse_erd_outcomes.lock docker exec lse-bot python scripts/backfill_event_reaction_outcomes.py --min-age-days 25 --limit 5000 >> ~/lse/logs/event_reaction_outcomes.log 2>&1
```

Скелет KB → `event_reaction_dataset` при необходимости **реже** (после массового импорта в KB):

```text
0 6 * * 0      docker exec lse-bot python scripts/build_event_reaction_dataset.py --from-kb-earnings --dataset-version v0
```

---

## Контроль (без новых дашбордов)

| Что смотреть | Где |
|--------------|-----|
| Доли `with_features_before`, `with_outcomes_after`, `labeled` | `GET /api/ml/data-quality` → `event_analytics` |
| Ошибки / охват котировок | лог-файлы джобов + счётчики `skip_no_quotes` в stdout |
| Смена поколения признаков | поле `feature_builder_version` внутри JSON + при необходимости новый `dataset_version` |

При падении доли features или росте ошибок — остановить cron, `--dry-run --limit 10`, сравнить выборочно 2–3 тикера с ручным расчётом.

---

## Что сделать в коде первым (минимальный PR-план)

1. `scripts/ingest_market_regime_daily.py` + UPSERT в `market_regime_daily` (если нужен режим в фичах с первого дня).  
2. `scripts/backfill_event_reaction_features.py` + `services/event_reaction_features.py` (только quotes-MVP, без peer).  
3. `scripts/backfill_event_reaction_outcomes.py` + `services/event_reaction_outcomes.py` (1d/5d/20d + простой `final_label`).  
4. Подключить строки в `setup_cron_docker.sh` / шаблон crontab (закомментированно, как остальные).  
5. Расширить `collect_event_analytics_stats` при желании полем `feature_builder_versions` (DISTINCT из jsonb) — опционально.

После этого блок **Event / earnings** в анализаторе начнёт отражать реальный прогресс без ручного SQL.

# Event / earnings: авторазметка `event_reaction_dataset`, cron и контроль

**Скелет строк** создаётся из KB: `scripts/build_event_reaction_dataset.py --from-kb-earnings`.  
**MVP авторазметка** (признаки до события, forward-исходы, rule-based `final_label`) из daily **`quotes`**: модуль `services/event_reaction_labeling.py`, CLI `scripts/backfill_event_reaction_labeling.py`.

**Зависимость от `quotes`:** скрипт разметки только читает PostgreSQL. Если в логах **`no_quotes`**, в таблице нет daily-ряда для этого `symbol`. Догрузка: **`scripts/seed_quotes_for_event_reaction_dataset.py`** (по умолчанию только символы из **TICKERS_FAST/MEDIUM/LONG**) или `python update_prices.py AAPL,MSFT --backfill 450`. Регулярный `update_prices_cron` не добавляет тикеры вне конфига.

**Universe датасета:** скелет из KB (`build_event_reaction_dataset.py`) **по умолчанию** вставляет только строки, где `kb.ticker` входит в тот же список (**FAST+MEDIUM+LONG**). Полный поток KB без фильтра: флаг **`--include-all-kb-tickers`**. Разметка и seed quotes по умолчанию тоже ограничены конфигом; снять ограничение: **`--include-all-symbols`** / **`--include-all-dataset-symbols`**.

Вспомогательные таблицы (`market_regime_daily`, `peer_graph_edge`, …) по-прежнему опциональны; их можно подключать в следующих версиях `feature_builder_version` внутри JSON.

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

## Авторазметка (MVP): `features_before`, `outcomes_after`, `final_label`

| Компонент | Назначение |
|-----------|------------|
| `services/event_reaction_labeling.py` | Якорь: последний торговый день с `date <=` дня события в **America/New_York**; из `quotes` — log-returns до/после close якоря; `final_label` UP/DOWN/FLAT по \|forward 5d log-ret\| vs порог |
| `scripts/backfill_event_reaction_labeling.py` | Батчевый `UPDATE` пустых (или с `--force-*`) строк |

**Версии в JSON:** `feature_builder_version` = `quotes_mvp_1`, `outcome_builder_version` = `quotes_fwd_1`. После смены формул — новая строка версии в JSON и/или новый `dataset_version` в таблице.

**Порог для меток:** конфиг `EVENT_REACTION_LABEL_THRESHOLD_LOG` (log-пространство); если не задан — используется тот же effective edge, что и `portfolio_ml_threshold_log()` (согласованность с портфельным ML). См. `config.env.example`.

**CLI (важные флаги):** `--dataset-version`, `--limit`, `--dry-run`, `--only-features`, `--only-outcomes`, `--force-features`, `--force-outcomes`, `--horizons 1,5,20`, `--id-from` / `--id-to`, `--since` / `--until` (ISO time, сравнение с `event_time_et`).

**Ограничения MVP:** нет peer/regime фич; для исходов нужно ≥5 торговых дней вперёд от якоря (иначе `outcomes_after` не пишется); для полноты признаков желательно ≥20 баров истории до якоря.

---

## Ручная правка разметки (когда нужна)

Авторазметка может ошибаться на корпоративных действиях, сплитах, тонком тайминге отчёта относительно якоря, или если нужен **другой** экономический смысл метки (например, горизонт не 5d). Тогда правят **источник правды в БД**:

1. Найти строку: по `id`, или по `(symbol, event_time_et, dataset_version)`.
2. Обновить JSON и/или метку; выставить **`label_source = 'manual'`**, чтобы отличить от `auto_quotes_v1`.

Пример (подставьте свой `id` и JSON; ключи внутри JSON должны соответствовать принятой схеме версии билдера):

```sql
UPDATE event_reaction_dataset
SET
  outcomes_after = '{"outcome_builder_version":"quotes_fwd_1","forward_log_ret_5d":0.012,"threshold_log_used":0.004}'::jsonb,
  final_label = 'UP',
  label_source = 'manual',
  updated_at = NOW()
WHERE id = 12345;
```

Точечная правка только сценария без пересчёта JSON (редко имеет смысл — рассинхрон с `outcomes_after`):

```sql
UPDATE event_reaction_dataset
SET final_label = 'FLAT', label_source = 'manual', updated_at = NOW()
WHERE id = 12345;
```

Правка **признаков** (например, после исправления котировок):

```sql
UPDATE event_reaction_dataset
SET
  features_before = features_before || '{"note":"manual_adjusted_as_of","feature_builder_version":"quotes_mvp_1"}'::jsonb,
  label_source = 'manual',
  updated_at = NOW()
WHERE id = 12345;
```

После массовых ручных правок имеет смысл зафиксировать выборку в отдельном `dataset_version` (например `v0_manual_q1`) экспортом/копированием строк, чтобы не смешивать с сырым авто-слоем.

---

## Обучение метрик, анализ, прод

Тот же контур, что и для остальных ML-задач в репозитории:

1. **Полнота данных:** `GET /api/ml/data-quality` → `event_analytics` (доли `with_features_before`, `with_outcomes_after`, `labeled`).
2. **Единый отчёт:** `python scripts/run_ml_data_quality_report.py` (см. [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md)).
3. **Обучение CatBoost** для event-слоя в коде пока не вынесено в отдельный скрипт; ориентир по структуре датасета/метрик — `scripts/train_portfolio_catboost.py` (log-returns, `--json-metrics-out`, пороговые метрики). Практически: выгрузка строк с непустыми `features_before` / `outcomes_after`, целевая переменная — например `forward_log_ret_5d` из `outcomes_after` или класс из `final_label`.
4. **Прод-инференс** — только после появления `.cbm`, гейтов в `ml_train_readiness.jsonl` и явного включения в конфиге/сервисе (как GAME_5M / портфель); до этого слой остаётся офлайн-аналитикой.

---

## Регулярность и cron (пример)

Порядок зависимостей: **котировки** → (опционально) вспомогательные таблицы → **авторазметка** `backfill_event_reaction_labeling.py` (можно два прохода: сначала `--only-features`, позже `--only-outcomes` для «созревших» по календарю строк).

Пример строк (хост, `docker exec lse-bot`, логи на volume):

```text
# MVP: признаки + исходы одним скриптом (строки без исходов останутся с частичным заполнением до появления forward-баров)
30 2 * * *     flock -n /tmp/lse_erd_label.lock docker exec lse-bot python scripts/backfill_event_reaction_labeling.py --dataset-version v0 --limit 3000 >> ~/lse/logs/event_reaction_labeling.log 2>&1
```

Опционально разнести нагрузку:

```text
32 2 * * *     flock -n /tmp/lse_erd_feat.lock docker exec lse-bot python scripts/backfill_event_reaction_labeling.py --only-features --dataset-version v0 --limit 5000 >> ~/lse/logs/event_reaction_features.log 2>&1
48 2 * * *     flock -n /tmp/lse_erd_out.lock docker exec lse-bot python scripts/backfill_event_reaction_labeling.py --only-outcomes --dataset-version v0 --limit 8000 >> ~/lse/logs/event_reaction_outcomes.log 2>&1
```

```text
# Режим рынка (если скрипт добавлен)
15 1 * * 1-5  flock -n /tmp/lse_market_regime.lock docker exec lse-bot python scripts/ingest_market_regime_daily.py >> ~/lse/logs/event_regime.log 2>&1
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

## Дальнейшее развитие (по желанию)

1. `scripts/ingest_market_regime_daily.py` + UPSERT в `market_regime_daily` (режим в фичах).  
2. Расширить `features_before`: peer-граф, `earnings_event_detail`, новая `feature_builder_version`.  
3. Отдельный `scripts/train_event_reaction_catboost.py` + гейты в `run_ml_train_readiness_cron.py` (по аналогии с портфелем / GAME_5M).  
4. `collect_event_analytics_stats`: опционально DISTINCT `feature_builder_version` из JSONB.

После стабильной авторазметки блок **Event / earnings** в анализаторе отражает реальный прогресс; ручные правки учитывайте через `label_source` и при необходимости отдельный `dataset_version`.

# Event / earnings: авторазметка `event_reaction_dataset`, cron и контроль

**Скелет строк** создаётся из KB: `scripts/build_event_reaction_dataset.py --from-kb-earnings`.  
**MVP авторазметка** (признаки до события, forward-исходы, rule-based `final_label`) из daily **`quotes`**: модуль `services/event_reaction_labeling.py`, CLI `scripts/backfill_event_reaction_labeling.py`.

**Зависимость от `quotes`:** скрипт разметки только читает PostgreSQL. Если в логах **`no_quotes`**, в таблице нет daily-ряда для этого `symbol`. Догрузка: **`scripts/seed_quotes_for_event_reaction_dataset.py`** (по умолчанию только символы **без ни одной** строки в `quotes`; если строки есть, но история короткая и старые события дают `no_quotes` — **`--all-symbols`** или **`--min-quote-span-days 320`**) или `python update_prices.py AAPL,MSFT --backfill 450`. Регулярный `update_prices_cron` не добавляет тикеры вне конфига.

**Universe датасета:** скелет из KB (`build_event_reaction_dataset.py`) **по умолчанию** вставляет только строки, где `kb.ticker` входит в тот же список (**FAST+MEDIUM+LONG**). Полный поток KB без фильтра: флаг **`--include-all-kb-tickers`**. Разметка и seed quotes по умолчанию тоже ограничены конфигом; снять ограничение: **`--include-all-symbols`** / **`--include-all-dataset-symbols`**.

**Старт «эры проекта» (без глубокой истории):** ограничить события с даты старта LSE, чтобы не тянуть котировки на 10–15 лет назад. Скелет из KB: **`--kb-since 2026-02-01`** или **`EVENT_REACTION_KB_SINCE`** в `config.env` (фильтр `kb.ts >= дата`). Разметка только по этому хвосту: **`backfill_event_reaction_labeling.py --since 2026-02-01`** (по колонке `event_time_et`). Уже вставленные старые строки скрипты не удаляют — при необходимости один раз `DELETE … event_time_et < '2026-02-01'` или новый **`dataset_version`**.

**Лишние тикеры в таблице** (после старого `--include-all-kb-tickers`): один раз **`build_event_reaction_dataset.py --prune-non-config --dataset-version v0`** (оставляет только FAST+MEDIUM+LONG). Сначала **`--dry-run`**.

Вспомогательные таблицы (`market_regime_daily`, `peer_graph_edge`, …) по-прежнему опциональны; их можно подключать в следующих версиях `feature_builder_version` внутри JSON.

**Product advisory dataset (с 2026-05-27):** для event-reaction CatBoost в карточках используется расширенная история **`v0_expanded_baseline`** с `feature_builder_version=quotes_regime_v1`. Она собрана из yfinance earnings history по 14 equity-тикерам FAST+MEDIUM+LONG: **498** событий, **451** trainable rows, 47,966 daily quote rows backfill.

**Earnings intelligence grid (с 2026-05-28):** отдельный контур для **сценарного классификатора** — `feature_builder_version=quotes_regime_earnings_v1` (quotes + regime + earnings tone/timing + peer graph topology + peer momentum). Оркестратор: **`scripts/run_earnings_ml_refresh.py`**. Метки: `label_source=llm_scenario_v0` из LLM `scenario_hints` (`apply_earnings_scenario_labels.py`). Train: **`train_event_reaction_scenario_classifier.py`**. Product-регрессия на `quotes_regime_v1` **не заменяется** этим слоем.

Дизайн-источник: [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md) §4.2.1.  
Официальные IR и страницы квартальной отчётности по тикерам (под будущий ingest и ручную работу): [earnings-event-agent-lse/PUBLIC_IR_EARNINGS_SOURCES.md](earnings-event-agent-lse/PUBLIC_IR_EARNINGS_SOURCES.md).

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
- **Реализация:** `scripts/ingest_market_regime_daily.py` — UPSERT из `quotes` (SPY/QQQ/DIA/^VIX; при отсутствии рядов — auto-seed через yfinance).
- **Регулярность:** cron **23:32 MSK** пн–пт (`crontab/lse-docker.crontab`), до `build_event_reaction_dataset` (23:33).
- **В признаках события:** `EVENT_REACTION_FEATURE_BUILDER_VERSION=quotes_regime_v1` (default) — `services/event_reaction_labeling.py` подмешивает `mkt_*` поля из `market_regime_daily` на `as_of_trade_date` и пишет `market_regime_date` в строку датасета. Старая версия: `quotes_mvp_1` (только тикер).

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

### `earnings_material`

- **Назначение:** реестр первичных материалов отчёта/call: IR event page, press release, presentation, transcript, follow-up, SEC/сторонний transcript URL.
- **Реализация:** `scripts/sync_earnings_material_registry.py` (KB EARNINGS + catalog URLs), `scripts/seed_earnings_material_registry.py` (legacy seed), `scripts/ingest_earnings_materials.py` + `services/earnings_material_parser.py` (HTML + pypdf).
- **LLM extraction:** `scripts/extract_earnings_material_facts.py` → `earnings_event_detail`; token audit: `scripts/audit_earnings_materials_pipeline.py --symbols META,NVDA`.
- **Регулярность:** cron — sync/ingest каждые 2 ч, extract каждые 6 ч (`crontab/lse-docker.crontab`).

---

## Авторазметка (MVP): `features_before`, `outcomes_after`, `final_label`

| Компонент | Назначение |
|-----------|------------|
| `services/event_reaction_labeling.py` | Якорь: последний торговый день с `date <=` дня события в **America/New_York**; из `quotes` — log-returns до/после close якоря; `final_label` UP/DOWN/FLAT по \|forward 5d log-ret\| vs порог |
| `scripts/backfill_event_reaction_labeling.py` | Батчевый `UPDATE` пустых (или с `--force-*`) строк |

**Версии в JSON:** `feature_builder_version` = `quotes_mvp_1`, `outcome_builder_version` = `quotes_fwd_1`. После смены формул — новая строка версии в JSON и/или новый `dataset_version` в таблице.

**Порог для меток:** конфиг `EVENT_REACTION_LABEL_THRESHOLD_LOG` (log-пространство); если не задан — используется тот же effective edge, что и `portfolio_ml_threshold_log()` (согласованность с портфельным ML). См. `config.env.example`.

**CLI (важные флаги):** `--dataset-version`, `--limit`, `--dry-run`, `--only-features`, `--only-outcomes`, `--force-features`, `--force-outcomes`, `--horizons 1,5,20`, `--id-from` / `--id-to`, `--since` / `--until` (ISO time, сравнение с `event_time_et`).

**Ограничения MVP:** для `quotes_regime_v1` — peer/regime через `market_regime_daily`; для **`quotes_regime_earnings_v1`** — добавлены tone, peer graph, peer momentum (см. `services/event_reaction_labeling.py`). Для исходов нужно ≥5 торговых дней вперёд от якоря; для полноты признаков желательно ≥20 баров истории до якоря. ~11% строк backfill могут дать `features:no_quotes` (seed quotes).

### `quotes_regime_earnings_v1` (earnings grid)

| Компонент | Назначение |
|-----------|------------|
| `enrich_features_with_peer_graph` | out-degree, sum weight по `peer_graph_edge` |
| `enrich_features_with_peer_momentum` | mean/max 5d log-ret пиров на `as_of` |
| tone / scenario hint counts | из `earnings_event_detail.guidance_summary` |
| `apply_earnings_scenario_labels.py` | top LLM scenario → `final_label`, `label_source=llm_scenario_v0` |
| `run_earnings_ml_refresh.py` | labels → backfill earnings_v1 → scenario classifier → readiness JSON |
| `train_event_reaction_scenario_classifier.py` | CatBoostClassifier multi-class, min 8 rows, artifact `event_reaction_scenario_catboost.cbm` |

Backfill earnings_v1 (без dry-run):

```bash
EVENT_REACTION_FEATURE_BUILDER_VERSION=quotes_regime_earnings_v1 \
python scripts/backfill_event_reaction_labeling.py \
  --dataset-version v0_expanded_baseline --only-features --force-features \
  --include-all-symbols --limit 300
```

Полный цикл grid:

```bash
ML_READINESS_TRAIN_MODE=full python scripts/run_earnings_ml_refresh.py --backfill-limit 300
```

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
3. **Обучение CatBoost:** `scripts/train_event_reaction_catboost.py` (регрессия на `forward_log_ret_5d`, `--json-metrics-out`, гейты в `run_ml_train_readiness_cron.py`). Для product advisory использовать `EVENT_REACTION_DATASET_VERSION=v0_expanded_baseline` и `EVENT_REACTION_FEATURE_BUILDER_VERSION=quotes_regime_v1`.
4. **Earnings grid (pilot):** `scripts/run_earnings_ml_refresh.py` → scenario classifier; readiness в `last_earnings_intelligence_readiness.json`; гейты в `/analyzer`.
5. **Прод-инференс** — текущий безопасный режим: **advisory/shadow only**. Включить `EVENT_REACTION_CATBOOST_ENABLED=true`, но оставить `EVENT_REACTION_BLOCK_BUY_ON_WEAK=false`, пока нет отдельного trading backtest / live shadow статистики. Scenario classifier **не** подключён к блокировке сделок.

---

## Регулярность и cron

**Эталон в репозитории:** `crontab/lse-docker.crontab` (ручная установка на хост) и **`setup_cron_docker.sh`** (генерация crontab из корня проекта).

**Materials + LLM (earnings intelligence):**

- **:18 */2** — `sync_earnings_material_registry.py`
- **:20 */2** — `ingest_earnings_materials.py`
- **:25 */6** — `extract_earnings_material_facts.py`
- **:30 */6** — `run_earnings_ml_refresh.py` (dry-run grid + readiness JSON)

**Event-reaction dataset (будни):**

- **23:33** — build skeleton в `v0_expanded_baseline`.
- **23:36** — backfill features/outcomes с `quotes_regime_v1`.
- **23:50** — readiness dry-run (GAME_5M + portfolio + event_reaction + **earnings grid** при `ML_READINESS_SKIP_EARNINGS_INTELLIGENCE=0`).
- **23:51** — full train только event-reaction regression (GAME_5M/portfolio skipped).
- **23:52** — full train **earnings grid** (`run_earnings_ml_refresh.py`, `ML_READINESS_TRAIN_MODE=full`).
- **23:53** — `run_ml_data_quality_report.py --no-default-datasets`.

Порядок зависимостей: **котировки** (`quotes`, в т.ч. `update_prices_cron`) → (опционально) seed → **build** → **backfill** (одним проходом заполняются пустые `features_before` и/или `outcomes_after`; при больших объёмах можно разнести `--only-features` / `--only-outcomes`).

Альтернативные сдвиги по времени (если не используете файлы из репо):

```text
30 2 * * *     flock -n /tmp/lse_erd_label.lock docker exec lse-bot python scripts/backfill_event_reaction_labeling.py --dataset-version v0 --limit 3000 >> ~/lse/logs/event_reaction_labeling.log 2>&1
```

```text
32 2 * * *     flock -n /tmp/lse_erd_feat.lock docker exec lse-bot python scripts/backfill_event_reaction_labeling.py --only-features --dataset-version v0 --limit 5000 >> ~/lse/logs/event_reaction_features.log 2>&1
48 2 * * *     flock -n /tmp/lse_erd_out.lock docker exec lse-bot python scripts/backfill_event_reaction_labeling.py --only-outcomes --dataset-version v0 --limit 8000 >> ~/lse/logs/event_reaction_outcomes.log 2>&1
```

```text
# Режим рынка (если скрипт добавлен)
15 1 * * 1-5  flock -n /tmp/lse_market_regime.lock docker exec lse-bot python scripts/ingest_market_regime_daily.py >> ~/lse/logs/event_regime.log 2>&1
```

Скелет KB → `event_reaction_dataset` при необходимости **реже** (если не гоняете nightly build из репо):

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

1. ~~**Peer reactions**~~ — частично в `quotes_regime_earnings_v1` + Event Brief spillover; дальше — validation / train features.
2. ~~**Scenario labels**~~ — pilot `llm_scenario_v0` + classifier; расширять по мере LLM extract.
3. **Live shadow report:** predicted scenario vs `forward_log_ret_5d` и peer spillover после созревания.
4. **Trading metric gate:** PnL/top-k после transaction costs; RMSE регрессии не единственный критерий.
5. **Materials coverage:** universe tickers без parsed materials (DELL, ANET, …).
6. **Event fusion:** склеить earnings grid с GAME_5M/portfolio после shadow-статистики.

После стабильной авторазметки блок **Event / earnings** в анализаторе отражает реальный прогресс; ручные правки учитывайте через `label_source` и при необходимости отдельный `dataset_version`.

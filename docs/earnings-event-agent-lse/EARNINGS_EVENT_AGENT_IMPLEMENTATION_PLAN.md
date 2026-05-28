# План внедрения event / earnings (рабочая версия)

**Статус:** рабочий документ; детализирует практические шаги поверх [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md).  
**Принципы проекта:** log-returns, издержки в симуляциях, данные в PostgreSQL — см. корневые правила репозитория.

**Фазы 1–5 (MVP ценового контура):** зафиксировано выполнение **2026-05-11 — 2026-05-12** (деплой на GCP, crontab, правки анализатора и readiness).  
**Product advisory ML:** зафиксировано **2026-05-27** — исторический earnings dataset расширен до `v0_expanded_baseline`; event-reaction CatBoost можно показывать в карточках/API как advisory/shadow сигнал, без hard-block сделок.

---

## Выполнение фаз 1–5 (зафиксировано)

| Фаза | Сделано |
|------|---------|
| **1. Исходы после созревания** | `backfill_event_reaction_labeling.py` в ночном cron (`crontab/lse-docker.crontab` / `setup_cron_docker.sh`); ручной прогон на VM; повтор для свежих строк до появления `forward_log_ret_5d`. |
| **2. Контроль качества** | `GET /api/ml/data-quality` → `event_analytics`; блок **«ML: готовность и качество данных»** в `templates/analyzer.html` (гейты, `last_*` метрики, `<details>` с пояснениями); поле **`ml_runtime`** в JSON API — **гейт ≠ автовыключение** `*_CATBOOST_ENABLED` в проде. |
| **3. Чистка хвоста (опционально)** | SQL в этом документе; на проде массовая чистка не обязательна — эра задаётся `EVENT_REACTION_KB_SINCE` / `--since` в backfill. |
| **4. Cron** | Эталон — **`crontab/lse-docker.crontab`** (будни, MSK после US): **23:33** build из KB, **23:36** backfill, **23:40** `run_daily_game5m_ml_pipeline.py`, **23:50** `run_ml_train_readiness_cron.py` (в т.ч. portfolio + при `-e ML_READINESS_SKIP_EVENT_REACTION=0` — event), **23:52** `run_ml_data_quality_report.py`, **23:53** `run_recovery_d4a_stats_cron.py`. При расхождении с текстом ниже — верить crontab в репо. |
| **5. Обучение + readiness** | `train_event_reaction_catboost.py` (регрессия `forward_log_ret_5d`, пороги строк/гейта см. коммиты 2026-05-12); запись `last_event_reaction_train_metrics.json`; гейт `event_reaction` в `ml_train_readiness.jsonl`. **Инференс event в сделках не подключён** — только пайплайн данных и мониторинг. |

Текущий product-слой: `v0_expanded_baseline` + `quotes_regime_v1` (**498** событий, **451** trainable rows, walk-forward RMSE ≈ **0.1056**). `earnings_event_detail` заполнен EPS/timing по yfinance, но `quotes_regime_earnings_v1` пока не выбран для product-модели: на expanded sample он нейтрален/слегка хуже baseline. `peer_graph_edge` и revenue/guidance features — следующий этап.

### Product advisory snapshot (2026-05-27)

| Область | Решение |
|---------|---------|
| Dataset | `v0_expanded_baseline` |
| Feature builder | `quotes_regime_v1` |
| Model artifact | `/app/logs/ml/models/event_reaction_forward5d_catboost.cbm` |
| Runtime mode | `EVENT_REACTION_CATBOOST_ENABLED=true` допустимо для карточек/API |
| Trade blocking | `EVENT_REACTION_BLOCK_BUY_ON_WEAK=false` до отдельного trading backtest |
| Retraining | nightly full train только event-reaction, GAME_5M/portfolio skipped |

---

## Стратегический план шефа (дизайн §2) — где мы и что дальше

Ориентир: [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md) §2 «Цели».

| # | Цель (кратко) | Статус на 2026-05-12 | Что можно сделать дальше | Где копить факты |
|---|----------------|----------------------|---------------------------|------------------|
| 1 | **Классификация сценария** (pullback, follow-through, fade, …) | **Пилот 2026-05-28:** LLM hints → `llm_scenario_v0` (15 labels); CatBoostClassifier `event_reaction_scenario_v0` на `quotes_regime_earnings_v1`. Rule UP/DOWN/FLAT сохранён для регрессии. | Дождаться первого prod train classifier; live shadow vs 5d outcomes; расширить labels по мере extract. | `apply_earnings_scenario_labels.py`, `train_event_reaction_scenario_classifier.py`, `run_earnings_ml_refresh.py` |
| 2 | **Кросс-отчёты** (A → группа B) | **Частично:** `peer_graph_edge` 96 рёбер; spillover 1d/5d log-ret в Event Brief и UI; peer momentum в `quotes_regime_earnings_v1`. | Peer reactions как validation / доп. features; event-study таблица в analyzer. | `earnings_event_brief.py`, `event_reaction_labeling.enrich_features_with_peer_*` |
| 3 | **Циклы и фазы** (групповой цикл, AI-chips и т.д.) | Не в `features_before` MVP. | Подключить `ticker_price_regime` / агрегаты по группе в builder v2. | `quotes` + правила из дизайна §4.5; при необходимости ручные метки фазы группы. |
| 4 | **Синхронность / ротация** (лидер vs laggard) | Не в event-ML; есть контекст корреляций для GAME_5M. | Явные признаки «лидер события» и отклик follower в `features_before`. | `knowledge_base` (тип события, кластер темы); цены peer’ов; время событий ET. |
| 5 | **Режим индекса** (NDX/SPY/VIX, veto) | Таблица `market_regime_daily` пуста; скрипт ingest в репо **ещё не добавлен** (см. [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md)). | Реализовать `ingest_market_regime_daily.py` + cron; затем ключи режима в фичах. | Дневные ряды индексов/VIX (тот же принцип, что `quotes`); breadth — если появится источник. |

Итого по стратегии: **MVP 1–5 закрывает «календарь + цена до/после + первая регрессия + мониторинг»** — это **фундамент** под цели §2, но **не заменяет** сценарную классификацию, кросс-эффекты и режим рынка из дизайна.

---

## Где накапливать факты для БД и моделей (практика)

| Хранилище | Что копить | Зачем |
|-----------|------------|--------|
| **`knowledge_base`** | EARNINGS-события, текст, `embedding`, при пилоте — структурированный `outcome_json` / ссылка на транскрипт | Ingest уже кормит `build_event_reaction_dataset`; дальше — RAG и поля для LLM. |
| **`earnings_material`** | Реестр первичных материалов отчёта/call: IR event page, press release, presentation, transcript, SEC/third-party URL, статус скачивания/парсинга | Вход для downloader/parser и LLM extractor; не смешиваем список источников с готовыми признаками модели. |
| **`event_reaction_dataset`** | Уже: строки событий, `features_before` (`quotes_mvp_1`), `outcomes_after` (1d/5d/20d log-ret), `final_label` | Единый материализованный датасет для CatBoost и контроля качества. |
| **`earnings_event_detail`** | EPS actual/estimate, surprise, timing; дальше revenue/guidance | EPS/timing уже загружены, но не улучшают expanded baseline; следующий практичный слой — revenue/guidance и peer reactions. |
| **`peer_graph_edge`** | `(source, target, relation_type, weight)` из конфига или корреляций **с явной постановкой под события** | Кросс-эффекты и «лидер → кластер». |
| **`market_regime_daily`** | Снимок режима по `trade_date` (индексы, VIX, флаги) | Veto / множитель риска по §2.5 дизайна. |
| **Вне БД (временно)** | Таблицы шефа по окнам, скриншоты сценариев, список «META capex → инфра» | Для проектирования полей и пилота разметки до автоматизации. |

---

## Связь с кодом и документами

| Область | Где в репо |
|---------|------------|
| Дизайн-концепция | [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md) |
| **IR и квартальные материалы по тикерам** (хабы, примеры кварталов; заготовка под ingest) | [PUBLIC_IR_EARNINGS_SOURCES.md](PUBLIC_IR_EARNINGS_SOURCES.md) |
| DDL `event_reaction_dataset`, `earnings_event_detail`, `earnings_material`, … | `scripts/sql/ml_event_analytics_schema.sql`, `scripts/migrate_ml_event_analytics.py` |
| Starter registry отчётных материалов | `scripts/seed_earnings_material_registry.py` |
| Fetch/parse earnings materials (HTML v0) | `scripts/ingest_earnings_materials.py`, `services/earnings_material_parser.py` |
| Catalog + KB sync materials | `services/earnings_material_catalog.py`, `scripts/sync_earnings_material_registry.py` |
| LLM extraction → earnings_event_detail | `services/earnings_material_extractor.py`, `scripts/extract_earnings_material_facts.py` |
| Peer graph seed | `services/peer_graph_catalog.py`, `scripts/seed_peer_graph_edges.py` |
| Token / coverage audit | `scripts/audit_earnings_materials_pipeline.py` |
| Scenario labels from LLM hints | `scripts/apply_earnings_scenario_labels.py` |
| Earnings ML grid refresh | `scripts/run_earnings_ml_refresh.py` |
| Scenario classifier (pilot) | `scripts/train_event_reaction_scenario_classifier.py` |
| Earnings readiness gates | `services/earnings_intelligence_readiness.py` |
| Earnings intelligence UI/API | `services/earnings_intelligence_api.py`, `/earnings`, `/api/earnings/*` |
| Event Brief JSON | `services/earnings_event_brief.py`, `scripts/build_earnings_event_brief.py` |
| Скелет из KB, фильтр конфига, «эра» по `kb.ts` | `scripts/build_event_reaction_dataset.py` (`--kb-since` / `EVENT_REACTION_KB_SINCE`) |
| Авторазметка ценами (MVP) | `services/event_reaction_labeling.py`, `scripts/backfill_event_reaction_labeling.py` |
| Котировки под датасет | `scripts/seed_quotes_for_event_reaction_dataset.py` (`--all-symbols`, `--min-quote-span-days`) |
| Качество / аналитика | `GET /api/ml/data-quality`, `scripts/run_ml_data_quality_report.py`, [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md), [ML_DATA_QUALITY_PIPELINE.md](../ML_DATA_QUALITY_PIPELINE.md) |
| Обучение CatBoost (MVP) | `scripts/train_event_reaction_catboost.py` (`EVENT_REACTION_DATASET_VERSION=v0_expanded_baseline` для product advisory) |
| Готовность к прод (опционально) | `scripts/run_ml_train_readiness_cron.py` (`ML_READINESS_SKIP_EVENT_REACTION`, метрики в `last_event_reaction_train_metrics.json`) |

---

## Источники данных (календарь vs содержимое)

### Сейчас (этапы 1–5)

- **Календарь и ценовой контур:** `knowledge_base` (EARNINGS из yfinance/др.) + **`quotes`** → `event_reaction_dataset` (`features_before`, `outcomes_after`, rule-based `final_label`). Этого достаточно для **MVP-меток и первой модели по ценовым фичам**.
- **Содержимое отчёта / earnings call** (тезисы шефа: tone, guidance, Q&A, кросс-эффект на peers) — **не блокирует** пункты 1–5. Хранение по дизайну: **`earnings_event_detail`** + текст/`embedding` в **`knowledge_base`**, см. §4.1–4.2.1 дизайн-дока.

### После 1–5 (отложено)

- **Автоматизация «тяжёлого» источника:** как правило **лицензированный API/подписка** (транскрипты, саммари, факты) или поддерживаемые **официальные** каналы (SEC EDGAR, IR RSS). Подключается отдельным ingest, когда зафиксирован шаблон полей и объём данных из пилота (1–2 темы, ручной/LLM-проход). Список стартовых URL по нашим тикерам: [PUBLIC_IR_EARNINGS_SOURCES.md](PUBLIC_IR_EARNINGS_SOURCES.md).

---

## Фазы 1–5 (текущая реализация)

### 1. Исходы после созревания

Периодически (или по cron, см. § «Cron»):

```bash
docker exec lse-bot python scripts/backfill_event_reaction_labeling.py \
  --dataset-version v0 --since 2026-02-01 --only-outcomes --limit 2000
```

Свежие события дают `insufficient_forward_for_5d`, пока не накопится 5 торговых дней вперёд от якоря — повторять.

### 2. Контроль качества

- `GET /api/ml/data-quality` → `event_analytics`.
- `python scripts/run_ml_data_quality_report.py` (при необходимости без тяжёлых CSV).

### 3. Чистка хвоста до «эры проекта» (опционально)

Скрипты не удаляют старые строки. Однократно при необходимости:

```sql
DELETE FROM event_reaction_dataset
WHERE dataset_version = 'v0' AND event_time_et < '2026-02-01';
```

Альтернатива: новый `dataset_version` (например `v0_from_feb26`) и сборка скелета только в него.

### 4. Cron (репозиторий)

Активные строки (будни, ~MSK после US):

- **`crontab/lse-docker.crontab`** — эталон для ручной установки `crontab …` на VM: **23:33** `build_event_reaction_dataset.py`, **23:36** `backfill_event_reaction_labeling.py` (`--since` / `EVENT_REACTION_KB_SINCE` под среду), **23:50** `run_ml_train_readiness_cron.py` с **`docker exec -e ML_READINESS_SKIP_EVENT_REACTION=0`** (train + `last_event_reaction_train_metrics.json` + гейт в JSONL для анализатора).
- **`setup_cron_docker.sh`** — тот же порядок с `$PROJECT_DIR` / `$CONTAINER_NAME`.

Порядок данных: дневные **`quotes`** (в т.ч. `update_prices_cron`) → build → backfill → readiness. При необходимости разнести `--only-features` / `--only-outcomes` — см. [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md).

### 5. Обучение CatBoost (MVP) + readiness

- **`scripts/train_event_reaction_catboost.py`**: строки с непустыми `features_before` и ключом `forward_log_ret_5d` в `outcomes_after`, регрессия в log-пространстве, **`--json-metrics-out`**, `--dry-run`.
- **`run_ml_train_readiness_cron.py`**: при **`ML_READINESS_SKIP_EVENT_REACTION=0`** дополнительно вызывает train, пишет **`last_event_reaction_train_metrics.json`**, гейт по `n_train` и `rmse_valid` (пороги через `ML_READINESS_EVENT_REACTION_*`). По умолчанию блок **пропущен**, чтобы не ломать существующий `overall_production_ready`.

---

## Следующий слой (после стабилизации 1–5)

**Выполнено 2026-05-28 (universe + UI + ML grid pilot):**

- Universe 21 equity, SEC auto-sources, materials cron, LLM extract на большинстве tickers.
- Web `/earnings`, Telegram `/earnings`, Event Brief + peer spillover, peer graph UI.
- `quotes_regime_earnings_v1` (tone + peer graph + peer momentum).
- `apply_earnings_scenario_labels` → 15 `llm_scenario_v0` labels на prod.
- `run_earnings_ml_refresh` + scenario classifier + analyzer readiness gates.
- Cron: `:30 */6` dry-run grid, `23:52` full train grid.

**В работе (2026-05-28):** первый prod full backfill `quotes_regime_earnings_v1` + train scenario classifier.

**Дальше:**

1. **Materials coverage** — DELL, ANET, AVGO, GOOGL, PLTR; auto-ensure KB в sync.
2. **Classifier quality** — OOS accuracy по walk-forward; не путать с RMSE регрессии.
3. **Live shadow** — predicted scenario vs realized 5d log-ret / peer spillover.
4. **Trading metric gate** — PnL после transaction costs перед fusion с GAME_5M.
5. **Revenue/guidance** — уже в LLM extractor; явные numeric features в builder v2 при необходимости.
6. **Подписка/IR/SEC** — замена ручного сбора для tickers без materials.

~~1. Materials registry + ingest~~ — ✅  
~~5. Scenario labels~~ — ✅ pilot  
~~6. Event Brief~~ — ✅ + UI

---

## Версия документа

| Версия | Дата | Изменение |
|--------|------|-----------|
| 0.1 | 2026-05-12 | Первая рабочая сборка: фазы 1–5, источники, ссылки на скрипты |
| 0.2 | 2026-05-12 | Добавлены `train_event_reaction_catboost.py`, readiness `event_reaction`, закомментированный cron в `setup_cron_docker.sh` |
| 0.3 | 2026-05-12 | Cron в `crontab/lse-docker.crontab` и `setup_cron_docker.sh`: build + backfill + readiness с train event_reaction (`-e ML_READINESS_SKIP_EVENT_REACTION=0`) |
| 0.4 | 2026-05-12 | Зафиксировано выполнение фаз 1–5 (11–12.05); сводка по целям §2 дизайна; таблица накопления фактов для БД/моделей |
| 0.5 | 2026-05-27 | Расширенный dataset `v0_expanded_baseline`, advisory product rollout, nightly event-reaction model refresh, roadmap revenue/guidance + peer features |
| 0.6 | 2026-05-28 | Добавлен `earnings_material` registry и starter seed для материалов earnings intelligence |
| 0.7 | 2026-05-28 | PDF ingest (pypdf), LLM extractor, peer_graph v0, cron materials pipeline; pilot META/NVDA extraction (~27k tok/event) |
| 0.8 | 2026-05-28 | Full universe, `/earnings` UI, `quotes_regime_earnings_v1`, scenario classifier pilot, `run_earnings_ml_refresh`, analyzer earnings grid readiness, cron grid refresh |

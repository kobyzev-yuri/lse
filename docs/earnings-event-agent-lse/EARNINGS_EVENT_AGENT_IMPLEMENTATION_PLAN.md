# План внедрения event / earnings (рабочая версия)

**Статус:** рабочий документ; детализирует практические шаги поверх [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md).  
**Принципы проекта:** log-returns, издержки в симуляциях, данные в PostgreSQL — см. корневые правила репозитория.

---

## Связь с кодом и документами

| Область | Где в репо |
|---------|------------|
| Дизайн-концепция | [EARNINGS_EVENT_AGENT_DESIGN.md](EARNINGS_EVENT_AGENT_DESIGN.md) |
| DDL `event_reaction_dataset`, `earnings_event_detail`, … | `scripts/sql/ml_event_analytics_schema.sql`, `scripts/migrate_ml_event_analytics.py` |
| Скелет из KB, фильтр конфига, «эра» по `kb.ts` | `scripts/build_event_reaction_dataset.py` (`--kb-since` / `EVENT_REACTION_KB_SINCE`) |
| Авторазметка ценами (MVP) | `services/event_reaction_labeling.py`, `scripts/backfill_event_reaction_labeling.py` |
| Котировки под датасет | `scripts/seed_quotes_for_event_reaction_dataset.py` (`--all-symbols`, `--min-quote-span-days`) |
| Качество / аналитика | `GET /api/ml/data-quality`, `scripts/run_ml_data_quality_report.py`, [EVENT_REACTION_PIPELINE.md](../EVENT_REACTION_PIPELINE.md), [ML_DATA_QUALITY_PIPELINE.md](../ML_DATA_QUALITY_PIPELINE.md) |
| Обучение CatBoost (MVP) | `scripts/train_event_reaction_catboost.py` |
| Готовность к прод (опционально) | `scripts/run_ml_train_readiness_cron.py` (`ML_READINESS_SKIP_EVENT_REACTION`, метрики в `last_event_reaction_train_metrics.json`) |

---

## Источники данных (календарь vs содержимое)

### Сейчас (этапы 1–5)

- **Календарь и ценовой контур:** `knowledge_base` (EARNINGS из yfinance/др.) + **`quotes`** → `event_reaction_dataset` (`features_before`, `outcomes_after`, rule-based `final_label`). Этого достаточно для **MVP-меток и первой модели по ценовым фичам**.
- **Содержимое отчёта / earnings call** (тезисы шефа: tone, guidance, Q&A, кросс-эффект на peers) — **не блокирует** пункты 1–5. Хранение по дизайну: **`earnings_event_detail`** + текст/`embedding` в **`knowledge_base`**, см. §4.1–4.2.1 дизайн-дока.

### После 1–5 (отложено)

- **Автоматизация «тяжёлого» источника:** как правило **лицензированный API/подписка** (транскрипты, саммари, факты) или поддерживаемые **официальные** каналы (SEC EDGAR, IR RSS). Подключается отдельным ingest, когда зафиксирован шаблон полей и объём данных из пилота (1–2 темы, ручной/LLM-проход).

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

1. Пилот **одного earnings call**: шаблон LLM → поля в `earnings_event_detail` / фрагмент в KB.  
2. **`affected_tickers` / peer_graph** под кейс «META capex → инфра/чипы».  
3. Подписка/поставщик как замена ручного сбора транскриптов.

---

## Версия документа

| Версия | Дата | Изменение |
|--------|------|-----------|
| 0.1 | 2026-05-12 | Первая рабочая сборка: фазы 1–5, источники, ссылки на скрипты |
| 0.2 | 2026-05-12 | Добавлены `train_event_reaction_catboost.py`, readiness `event_reaction`, закомментированный cron в `setup_cron_docker.sh` |
| 0.3 | 2026-05-12 | Cron в `crontab/lse-docker.crontab` и `setup_cron_docker.sh`: build + backfill + readiness с train event_reaction (`-e ML_READINESS_SKIP_EVENT_REACTION=0`) |

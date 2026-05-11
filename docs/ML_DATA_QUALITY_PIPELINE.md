# Конвейер разметки, ML и контроля качества данных

Документ связывает **подготовку данных в PostgreSQL**, **обучение CatBoost**, **метрики** и **единый отчёт** с опциональным **LLM-анализатором** применимости к задачам LSE (GAME_5M, портфель, `knowledge_base` / earnings, `event_reaction_dataset`, recovery). Доходности в моделях и отчётах — **log-returns**; симуляции — с **transaction costs** (правила проекта).

## 1. Цели

1. Видеть **полноту** разметки: `context_json` на BUY, `outcome_json` / embedding в KB, покрытие `quotes`.
2. Видеть **event ML слой**: наличие таблиц миграции, строки в `event_reaction_dataset`, доля `final_label` и заполненных `features_before` / `outcomes_after`.
3. Видеть **качество** датасетов: CSV из `build_game5m_*`, доли пропусков, наличие файлов.
4. Видеть **состояние ML-артефактов**: `.meta.json` у CatBoost, хвосты `portfolio_daily_ml_report.jsonl`, `game5m_daily_ml_report.jsonl`, **`ml_train_readiness.jsonl`**.
5. Получать **один JSON** и при необходимости **LLM-резюме** (готовность, пробелы, следующие шаги, приоритет ручной разметки).
6. **Регулярно** прогонять dry-run (или полное) обучение и фиксировать **гейты готовности к продакшену** (`run_ml_train_readiness_cron.py`).

## 2. Точка входа: единый отчёт

| Компонент | Путь |
|-----------|------|
| Сборщик метрик | `services/ml_data_quality_report.py` |
| LLM-слой | `services/ml_data_quality_llm.py` |
| CLI | `scripts/run_ml_data_quality_report.py` |

Примеры:

```bash
# Только машинный JSON (БД + датасеты + мета моделей + event_analytics)
python scripts/run_ml_data_quality_report.py --json-out local/logs/ml_data_quality/report.json

# Плюс dry-run GAME_5M и портфеля → JSON метрик в /app/logs/ml/ml_data_quality/ (или local/...)
python scripts/run_ml_data_quality_report.py \
  --game5m-train-dry-run --portfolio-train-dry-run \
  --json-out local/logs/ml_data_quality/report.json

# Плюс LLM-оценка (ключ в config.env / ProxyAPI — см. .cursorrules)
python scripts/run_ml_data_quality_report.py --json-out local/logs/ml_data_quality/report.json --llm --print-llm-summary
```

Дополнительные CSV для профиля:

```bash
python scripts/run_ml_data_quality_report.py --dataset path/to/custom.csv --json-out report.json
```

Флаг `--no-default-datasets` отключает профилирование `local/datasets/game5m_*` по умолчанию.

## 3. Разметка и данные (что контролируем)

| Источник | Поля / смысл | Инструменты подготовки |
|----------|----------------|-------------------------|
| `trade_history` | `context_json` на BUY, ключ `decision`, стратегия | Крон GAME_5M / портфель; см. [GAME_5M_DEAL_PARAMS_JSON.md](GAME_5M_DEAL_PARAMS_JSON.md) |
| `knowledge_base` | `event_type`, `outcome_json`, `embedding` | Кроны новостей/earnings, `scripts/sync_vector_kb_cron.py`; см. [KNOWLEDGE_BASE_FIELDS.md](KNOWLEDGE_BASE_FIELDS.md) |
| `event_reaction_dataset` | `features_before`, `outcomes_after`, `final_label` | Миграция; skeleton: `build_event_reaction_dataset.py`; авторазметка из `quotes`: **`scripts/backfill_event_reaction_labeling.py`** (`services/event_reaction_labeling.py`), см. [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md) |
| `quotes` | Покрытие тикеров | Сидеры/Yahoo; см. [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) |
| CSV датасеты | Stuck / continuation | `scripts/build_game5m_stuck_dataset.py`, `scripts/build_game5m_continuation_dataset.py` |
| Recovery ML | JSONL экспорт | Анализатор `export_recovery_ml`; см. [GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md](GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md) |

Отчёт `build_ml_data_quality_report` **не изменяет** БД — только читает и профилирует.

## 3.1. Feature builder: что это и как используется в датасете

**Feature builder** — это **модуль кода** (отдельный сервис/скрипт/крон), который для строки события в **`event_reaction_dataset`** (якорь `symbol`, `event_time_et`):

- забирает из **PostgreSQL** сырые ряды и справочники: `quotes`, `premarket_daily_features`, `market_regime_daily`, `peer_graph_edge` (+ цены peer’ов), при необходимости факты из `earnings_event_detail` / ссылку на `knowledge_base_id`;
- вычисляет **признаки до события** (log-returns, волатильность, фаза цены, peer confirmation, бенчмарк и т.д. — см. [EARNINGS_EVENT_AGENT_DESIGN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md) §4.2 и §4.2.1);
- **сохраняет** результат в колонку **`features_before`** (JSONB). Исходы по горизонту — в **`outcomes_after`**; сценарий — в **`final_label`**.

**Использование:** обучение ML и офлайн-метрики читают **готовые JSONB из БД** (по `dataset_version` и версии внутри JSON), без обязательного повторного JOIN всех источников на каждый прогон. CSV-экспорт при необходимости — производная от таблицы, не источник правды.

Подробности и таблица потребителей: **§4.2.1** того же дизайн-документа.

## 4. DDL event / earnings analytics

| Шаг | Команда |
|-----|---------|
| Создать таблицы | `python scripts/migrate_ml_event_analytics.py` |
| SQL на просмотр | `scripts/sql/ml_event_analytics_schema.sql` |
| Skeleton из KB (EARNINGS*) | `python scripts/build_event_reaction_dataset.py --from-kb-earnings --dataset-version v0` |
| Авторазметка features/outcomes/label (MVP) | `python scripts/backfill_event_reaction_labeling.py --dataset-version v0 --limit 500` |
| Догрузка `quotes` для тикеров датасета (если `no_quotes`) | `python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --days 450` |
| План cron, ручная правка, обучение/метрики | [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md) |

Таблицы: `earnings_event_detail`, `peer_graph_edge`, `market_regime_daily`, `event_reaction_dataset`. Дамп: см. `scripts/export_pg_dump.sh` (список `LSE_TABLES` дополнен).

## 5. Обучение и метрики (скрипты)

| Скрипт | Назначение |
|--------|------------|
| `scripts/train_game5m_catboost.py` | Entry CatBoost; `--dry-run`, **`--json-metrics-out`** |
| `scripts/train_portfolio_catboost.py` | Портфель; `--dry-run`, **`--json-metrics-out`**, плюс append в `PORTFOLIO_ML_REPORT_JSONL` |
| `scripts/train_game5m_recovery_catboost.py` | Recovery по JSONL |
| `scripts/run_daily_game5m_ml_pipeline.py` | Stuck + continuation CSV + CatBoost + `game5m_daily_ml_report.jsonl` |
| **`scripts/run_ml_train_readiness_cron.py`** | Регулярный dry-run/full train + **гейты** + append **`ml_train_readiness.jsonl`** |

См. также [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md), [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md).

### 5.1 Готовность к продакшену (гейты)

`run_ml_train_readiness_cron.py` пишет строку с `game5m.gate`, `portfolio.gate`, `overall_production_ready`.

Переменные окружения (основные):

| Переменная | Смысл |
|------------|--------|
| `ML_READINESS_JSONL` | Путь JSONL (default: `.../ml_train_readiness.jsonl`) |
| `ML_READINESS_TRAIN_MODE` | `dry_run` (по умолчанию) или `full` — перезапись `.cbm` |
| `ML_READINESS_SKIP_GAME5M` / `ML_READINESS_SKIP_PORTFOLIO` | `1` — пропустить модель |
| `ML_READINESS_GAME5M_AUC_MIN` | Порог AUC valid (default `0.52`) |
| `ML_READINESS_GAME5M_MIN_TRAIN` | Мин. `n_train` (default `40`) |
| `ML_READINESS_PORTFOLIO_RMSE_MAX` | Макс. RMSE valid (default `0.08`) |
| `ML_READINESS_PORTFOLIO_MIN_TRAIN` | Мин. `n_train` (default `80`) |

В Docker контейнер `lse-bot` получает `config.env` как **файл** `/app/config.env` (volume), без `env_file` в compose — переменные `ML_READINESS_*` в `docker exec … env` могут быть пустыми; скрипт `run_ml_train_readiness_cron.py` читает пороги через `config_loader` (env процесса **или** файл).

В веб-интерфейсе на странице **`/analyzer`** блок «ML: готовность…» и JSON **`GET /api/ml/data-quality`** (тот же сборщик, что `run_ml_data_quality_report`, без профилирования `local/datasets/*.csv` для скорости).

### 5.2 Включить инференс в проде (после зелёного readiness)

Гейты проверяют **dry-run** метрики; в рантайме модели по умолчанию **выключены** (`GAME_5M_CATBOOST_ENABLED` / `PORTFOLIO_CATBOOST_ENABLED`).

1. **Обновить артефакты** (если нужны свежие `.cbm` под текущие данные):  
   `ML_READINESS_TRAIN_MODE=full python scripts/run_ml_train_readiness_cron.py`  
   (осторожно: перезапишет модели по путям из `train_*` скриптов, обычно `/app/logs/ml/models/*.cbm`).
2. В **`config.env`** на хосте (монтируется в контейнер):  
   - `GAME_5M_CATBOOST_ENABLED=true`  
   - `PORTFOLIO_CATBOOST_ENABLED=true`  
   Пути к файлам уже задаются в примере: `GAME_5M_CATBOOST_MODEL_PATH`, `PORTFOLIO_CATBOOST_MODEL_PATH`.
3. **GAME_5M fusion** (опционально): по умолчанию `GAME_5M_CATBOOST_FUSION=none` — только вероятность в ответе, **правила входа не меняются**. Для осторожного влияния на вход: `hold_if_buy_below_p` + `GAME_5M_CATBOOST_HOLD_BELOW_P` — см. `docs/GAME_5M_CATBOOST_FUSION.md`.
4. Портфельная модель — **только advisory** (карточки / API), исполнение сделок ею не подменяется.
5. `docker compose restart lse` (или деплой), затем в логах/карточках проверить `catboost_signal_status` / `portfolio_ml_status`.

Шаблон строк см. **`config.env.example`** (блок «Включение ML в проде»).

**Пример cron** (после сессии, без смены моделей): на хосте с `lse-bot` — **`setup_cron_docker.sh`** (будни **23:50** MSK — readiness, **23:52** — `run_ml_data_quality_report.py --no-default-datasets`); см. также `crontab/lse-docker.crontab`.

```bash
docker compose exec -T lse python3 scripts/run_ml_train_readiness_cron.py >> /app/logs/ml_train_readiness_cron.log 2>&1
```

## 6. LLM-блок в отчёте

`analyze_ml_data_quality_with_llm` передаёт модели **только** сформированный JSON. В `applicability` учитывай блок **`event_analytics`** (таблицы и `event_reaction_dataset`).

Таймаут HTTP: `OPENAI_TIMEOUT_PROMPT_ENTRY` при наличии.

## 7. Связь с event / earnings

План сценариев: [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md). Отчёт качества добавляет **`event_analytics`**: строки датасета, разметка, версии `dataset_version`.

## 8. Версионирование

Поле `report_version` в корне JSON (`services/ml_data_quality_report.REPORT_VERSION`). Текущее: **1.1** (event_analytics + хвосты readiness / game5m daily).

# Конвейер разметки, ML и контроля качества данных

Документ связывает **подготовку данных в PostgreSQL**, **обучение CatBoost**, **метрики** и **единый отчёт** с опциональным **LLM-анализатором** применимости к задачам LSE (GAME_5M, портфель, `knowledge_base` / earnings, recovery). Доходности в моделях и отчётах — **log-returns**; симуляции — с **transaction costs** (правила проекта).

## 1. Цели

1. Видеть **полноту** разметки: `context_json` на BUY, `outcome_json` / embedding в KB, покрытие `quotes`.
2. Видеть **качество** датасетов: CSV из `build_game5m_*`, доли пропусков, наличие файлов.
3. Видеть **состояние ML-артефактов**: `.meta.json` у CatBoost, последние записи `portfolio_daily_ml_report.jsonl`.
4. Получать **один JSON** и при необходимости **LLM-резюме** (готовность, пробелы, следующие шаги, приоритет ручной разметки).

## 2. Точка входа: единый отчёт

| Компонент | Путь |
|-----------|------|
| Сборщик метрик | `services/ml_data_quality_report.py` |
| LLM-слой | `services/ml_data_quality_llm.py` |
| CLI | `scripts/run_ml_data_quality_report.py` |

Примеры:

```bash
# Только машинный JSON (БД + датасеты + мета моделей)
python scripts/run_ml_data_quality_report.py --json-out local/logs/ml_data_quality/report.json

# Плюс dry-run обучения GAME_5M (нужен catboost в окружении) и запись метрик в JSON
python scripts/run_ml_data_quality_report.py --game5m-train-dry-run --json-out local/logs/ml_data_quality/report.json

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
| `quotes` | Покрытие тикеров | Сидеры/Yahoo; см. [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) |
| CSV датасеты | Stuck / continuation | `scripts/build_game5m_stuck_dataset.py`, `scripts/build_game5m_continuation_dataset.py` |
| Recovery ML | JSONL экспорт | Анализатор `export_recovery_ml`; см. [GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md](GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md) |

Отчёт `build_ml_data_quality_report` **не изменяет** БД — только читает и профилирует.

## 4. Обучение и метрики (существующие скрипты)

| Скрипт | Назначение |
|--------|------------|
| `scripts/train_game5m_catboost.py` | Entry CatBoost; `--dry-run`, **`--json-metrics-out`** → JSON с `n_*`, `auc_valid`, статус `insufficient_rows` |
| `scripts/train_portfolio_catboost.py` | Дневной портфель; `--dry-run` пишет строку в `PORTFOLIO_ML_REPORT_JSONL` |
| `scripts/train_game5m_recovery_catboost.py` | Recovery по JSONL |
| `scripts/run_daily_game5m_ml_pipeline.py` | Stuck + continuation CSV + CatBoost + строка в `game5m_daily_ml_report.jsonl` |

См. также [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md), [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md) (операционный разбор сделок + LLM).

## 5. LLM-блок в отчёте

`analyze_ml_data_quality_with_llm` передаёт модели **только** сформированный JSON (без выдуманных чисел). Ответ — структурированный JSON: `summary_ru`, `completeness`, `quality_risks`, `applicability` по контурам, `recommended_next_steps`, `human_labeling`.

Таймаут HTTP берётся как у тяжёлых промптов (`OPENAI_TIMEOUT_PROMPT_ENTRY` при наличии).

## 6. Связь с event / earnings

План разметки сценариев и `outcome_json`: [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md). Отчёт качества показывает **долю заполненных** `outcome_json` по KB — это прокси готовности к `event_reaction_dataset`.

## 7. Версионирование

Поле `report_version` в корне JSON отчёта (`services/ml_data_quality_report.REPORT_VERSION`). При изменении схемы — поднять версию и кратко описать в коммите.

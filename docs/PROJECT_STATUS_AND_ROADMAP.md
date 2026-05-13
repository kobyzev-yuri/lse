# Статус планов и дорожная карта

**Обновлено:** 2026-05-13 (срез по репозиторию `lse` и эталонному `crontab/lse-docker.crontab`).

Этот файл — **единая точка входа**: что уже сделано, где мы сейчас, что осталось. Детальные чеклисты остаются в профильных документах (ссылки ниже).

---

## Сводка: сделано

| Направление | Состояние | Где подробности |
|-------------|-----------|-----------------|
| **GAME_5M ML nightly** | Stuck + continuation CSV, entry CatBoost, JSONL метрик; крон **23:40 MSK** пн–пт | `scripts/run_daily_game5m_ml_pipeline.py`, `crontab/lse-docker.crontab` |
| **ML readiness + data-quality** | Крон **23:50** / **23:52** — dry-run/full train по env, отчёт качества | `scripts/run_ml_train_readiness_cron.py`, `scripts/run_ml_data_quality_report.py` |
| **Recovery TIME_EXIT** | Фазы A–C, D1–D3, **D4a log-only** + JSONL в SELL; **rollup τ×K** + shallow окон + `window_suggestion`; крон **23:53** | `docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md` |
| **Event / earnings MVP** | Build KB → backfill quotes; **train_event_reaction** в readiness с `-e ML_READINESS_SKIP_EVENT_REACTION=0`; анализатор / `/api/ml/data-quality` | `docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md` |
| **Четыре CatBoost-сетки (док)** | Описание целей 5m / portfolio / recovery / event | `docs/TRADE_ML_DATASETS_AND_TARGETS_RU.md` |
| **Аудит config (локально)** | `code_to_example` доведён до 0; артефакты в `docs/audit_*.txt` | `docs/CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md` |

---

## Сводка: в работе / следующий шаг

| Направление | Статус «сейчас» | Что осталось |
|-------------|-----------------|--------------|
| **Recovery D4b** | Не внедрён; торговля как до D4a | Решение go/no-go по SQL + rollup + `recovery_scenario_backtest`; затем PR с defer K баров | `GAME_5M_TIME_EXIT_RECOVERY_PLAN.md` § D4b |
| **Event фазы «после MVP»** | Таблицы `market_regime_daily`, `peer_graph_edge`, `earnings_event_detail` **пустые**; CatBoost только **5d** таргет | `ingest_market_regime_daily.py` + cron; peer-фичи; опционально multi-horizon train | `EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md` § стратегические цели |
| **Hanger / continuation** | Датасеты + анализатор; **отдельного** `train_*_stuck` нет | По данным — модели stuck/continuation или правила continuation gate из log-only | `GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md` фазы 4–5 |
| **CatBoost entry «измерить эффект»** | Fusion включён осторожно (недельный план) | Метрики BUY→HOLD, walk-forward — вручную/через анализатор; при желании — агрегаты в отчёт | `WEEKLY_PLAN_GAME5M_AND_ML_2026-05-06.md` |
| **Аудит prod config** | Локальный example выровнен | Повтор `audit_config_unused_keys.py` против **боевого** `/app/config.env`, вычистить мёртвые ключи на VM | `CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md` § Завтра на сервере |

---

## Долго не продвигались (явно «висящие» направления)

Ниже — не баги, а **намеренно отложенные** или без владельца в коде:

1. **`ingest_market_regime_daily.py`** — в `EVENT_REACTION_PIPELINE.md` описан, **скрипта в репо пока нет**; без него режим рынка не попадёт в event-фичи.
2. **Recovery `run_daily_game5m_recovery_pipeline.py`** — не вынесен в отдельную строку `lse-docker.crontab`; обучение recovery зависит от ручного JSONL + запуска скрипта (или будущего крона).
3. **Фаза 5 hanger-плана** — условный **pyramid / докуп** при `GAME_5M_ALLOW_PYRAMID_BUY=false`; обсуждалось, в код правил не внедрялось.
4. **Отдельный nightly-cron только под `train_portfolio_catboost`** — в weekly-плане фигурировал как идея; фактически портфель гоняется из **`run_ml_train_readiness_cron`** (23:50), не дублируя отдельный `portfolio_daily_ml_pipeline` в crontab — это нормально, но лог `portfolio_daily_ml_pipeline.log` появится только если где-то вызывают полный train portfolio отдельно.

---

## Согласованность документов и архив

- **Не переносили** в `docs/archive/`: `CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md` — в нём ещё открытые `[ ]` по prod; вместо дублирования вверху плана добавлена **отсылка сюда**.
- **`WEEKLY_PLAN_GAME5M_AND_ML_2026-05-06.md`** — оставлен как **исторический снимок** недели; актуальный приоритет — этот файл.
- **`GAME_5M_TIME_EXIT_RECOVERY_PLAN.md`** — остаётся **источником правды** по recovery; D4a rollup/shallow уже отражены в § D4.
- Дублирование cron-времён между `EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md` и `crontab/lse-docker.crontab`: при расхождении верить **файлу crontab** в репо.

---

## Полезные ссылки

- Recovery: `docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md`
- Hanger / stuck / continuation: `docs/GAME_5M_HANGER_AND_STALE_EXIT_PLAN.md`
- Event: `docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md`, `docs/EVENT_REACTION_PIPELINE.md`
- ML качество: `docs/ML_DATA_QUALITY_PIPELINE.md`
- Навигация: `docs/README.md`

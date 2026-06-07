# Статус планов и дорожная карта

**Обновлено:** 2026-06-07 (event anchors + vol-scaled labels + NDX в technical params; earnings full refresh на prod).

**Каноническая архитектура ML и торговых решений:** [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md).  
**Словарь терминов (L1/L2/L3, BMO/AMH, open-path…):** [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md).  
**План консолидации (устранение дублей):** [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md).  
**Полный отчёт по контурам (dual-track legacy + stack):** [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md).

Этот файл — **живой ops-статус** (что сделано / что висит). Матрица контуров, cron и слои L1–L3 — в каноне и ML_STATUS_REPORT; здесь — краткая сводка.

---

## Сводка: сделано

| Направление | Состояние | Где подробности |
|-------------|-----------|-----------------|
| **Unified ML retrain (L1)** | **8/8** контуров active refresh, dispatcher nightly `23:47` + weekly `06:05`, prod full train 2026-06-05 | `6607dad`, [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md) |
| **ML readiness + data-quality (L2)** | Крон 23:50 / 23:53, analyzer, `/api/ml/data-quality` | [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md) |
| **Decision stack (L3)** | Dual-track: legacy исполняет; stack shadow (`RESOLVE=false`) | [ML_STATUS_REPORT.md](ML_STATUS_REPORT.md) §1 |
| **Earnings autoprep + grid** | Autoprep `15 */2`, full train 23:52, shadow gates | [OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md](OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md) |
| **Open-path MVP** | Labels 23:45, nightly 23:46, dispatcher poll | канон §5 |
| **GAME_5M ML nightly** | Legacy 23:40 datasets + entry; dispatcher incremental | [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md) |
| **Recovery TIME_EXIT** | D4a log-only + JSONL; крон 23:54 | [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md) |
| **Event / earnings MVP** | ERD build 23:33–37; leak-safe якоря BMO/AMH; vol-scaled labels; peer spillover calendar; full refresh 2026-06-07 | [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md), [ML_GLOSSARY_RU.md](ML_GLOSSARY_RU.md) §4 |
| **NDX в technical params** | `^NDX` в `market_regime_daily`, `ndx_gap_pct` в recommend_5m | `8455cc0`, канон §2 |
| **Анализатор** | `light=1`, async API, `ml_production_arbiter`, ML contours table | [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md) |

---

## Сводка: в работе (приоритет по consolidation plan)

| Направление | Статус | Следующий шаг | Фаза plan |
|-------------|--------|---------------|-----------|
| **L3 promotion** | Спринт 3.1 завершён | Только portfolio promoted; остальное defer/telemetry | 3 ✅ упор |
| **Portfolio CatBoost** | L2✅ L3✅ | Мониторинг RMSE/edge | 3 ✅ |
| **GAME_5M entry CatBoost** | AUC≈0.50, n_valid=45 | Defer до ≥80 valid rows | 3 (заблокирован) |
| **Multiday LR gates** | entry `apply` monitoring; hold log_only 3/5 | Hold apply не включать | 3 |
| **Gap forecast ML** | naive baseline лучше ridge | Caution, не L3 promotion | 3 |
| **L3 resolve** | RESOLVE=false, 5/41 session div (14d) | Weekly cron mirror; toggle вручную | 3 |
| **Recovery D4b** | 15 TE / 13 gate | Defer 2–4 нед | 3 |
| **Event-reaction** | RMSE gate ❌; grid/peer gates ✅ после refresh | Advisory only; autoprep ждёт ≥40 LLM labels | 3 |
| **Prod config audit** | Локальный example OK | Audit боевого `/app/config.env` | 4 |

### Prod ML full train (2026-06-05)

Коммиты: `6607dad` (L1 consolidation), `730db19` (archive), `8a03ea9` (recovery `--jsonl`). Все 8 контуров: `train_ran=True`. CatBoost: entry AUC≈0.50, portfolio RMSE≈0.078, recovery AUC≈0.71, event RMSE≈0.13. Multiday: **927** ridge JSON (merged universe).

### Mirror baseline (фаза 3.0, 2026-06-05)

7d: 32 snap / 4 div (12.5%). 14d: 41 snap / 5 div (12.2%) — все **session_veto**. `unexpected_divergence=0`. Политика: legacy исполняет; cron вс 06:10 MSK. Артефакт: `last_decision_stack_mirror_report.json`.

---

## Долго отложенные (не баги)

1. **Event-reaction hard-block** — ждёт trading backtest + transaction costs.
2. **Peer/detail таблицы** — частично заполнены.
3. **Recovery отдельный nightly cron** — закрыто: weekly `06:05` dispatcher + `run_recovery_ml_refresh.py` (фаза 2 ✅).
4. **Pyramid / докуп** — `GAME_5M_ALLOW_PYRAMID_BUY=false`, обсуждалось, не внедрено.

---

## Согласованность документов

| Правило | Действие |
|---------|----------|
| Матрица контуров / cron | Только [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md) + `crontab/lse-docker.crontab` |
| Dated планы (`EARNINGS_PLAN_*`, weekly) | Архив + stub → канон / PROJECT_STATUS |
| Feature plans (recovery D4, multiday gates) | Остаются; баннер «не канон по контурам» |
| Расхождение markdown vs crontab | Верить **crontab** |

---

## Полезные ссылки

- Канон ML + решения: [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md)
- Консолидация: [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md)
- Навигация: [README.md](README.md)
- Recovery: [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md)
- Earnings: [earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md](earnings-event-agent-lse/EARNINGS_EVENT_AGENT_IMPLEMENTATION_PLAN.md)

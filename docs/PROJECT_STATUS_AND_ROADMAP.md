# Статус планов и дорожная карта

**Обновлено:** 2026-06-05 (фазы 0–2 consolidation закрыты; фаза 3 L3 — в работе).

**Каноническая архитектура ML и торговых решений:** [ML_AND_DECISION_ARCHITECTURE.md](ML_AND_DECISION_ARCHITECTURE.md).  
**План консолидации (устранение дублей):** [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md).

Этот файл — **живой ops-статус** (что сделано / что висит). Матрица контуров, cron и слои L1–L3 — только в каноне, здесь не дублируются.

---

## Сводка: сделано

| Направление | Состояние | Где подробности |
|-------------|-----------|-----------------|
| **Unified ML retrain (L1)** | **8/8** контуров active refresh, dispatcher nightly `23:47` + weekly `06:05`, prod full train 2026-06-05 | `6607dad`, [ML_UNIFIED_RETRAIN_FRAMEWORK.md](ML_UNIFIED_RETRAIN_FRAMEWORK.md) |
| **ML readiness + data-quality (L2)** | Крон 23:50 / 23:53, analyzer, `/api/ml/data-quality` | [ML_DATA_QUALITY_PIPELINE.md](ML_DATA_QUALITY_PIPELINE.md) |
| **Decision stack (L3)** | Фазы 0–3 в коде; prod `DECISION_STACK_RESOLVE_ENABLED=false` (mirror) | [GAME_5M_DECISION_ARCHITECTURE.md](GAME_5M_DECISION_ARCHITECTURE.md) |
| **Earnings autoprep + grid** | Autoprep `15 */2`, full train 23:52, shadow gates | [OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md](OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md) |
| **Open-path MVP** | Labels 23:45, nightly 23:46, dispatcher poll | канон §5 |
| **GAME_5M ML nightly** | Legacy 23:40 datasets + entry; dispatcher incremental | [ML_GAME5M_CATBOOST.md](ML_GAME5M_CATBOOST.md) |
| **Recovery TIME_EXIT** | D4a log-only + JSONL; крон 23:54 | [GAME_5M_TIME_EXIT_RECOVERY_PLAN.md](GAME_5M_TIME_EXIT_RECOVERY_PLAN.md) |
| **Event / earnings MVP** | ERD build 23:33–37, regression refresh 23:51 | [EVENT_REACTION_PIPELINE.md](EVENT_REACTION_PIPELINE.md) |
| **Анализатор** | `light=1`, async API, `ml_production_arbiter`, ML contours table | [TRADE_EFFECTIVENESS_ANALYZER.md](TRADE_EFFECTIVENESS_ANALYZER.md) |

---

## Сводка: в работе (приоритет по consolidation plan)

| Направление | Статус | Следующий шаг | Фаза plan |
|-------------|--------|---------------|-----------|
| **L3 promotion (спринт 3.0)** | Baseline mirror | `report_decision_stack_mirror.py` + 7d shadow diff | 3 |
| **Portfolio CatBoost** | L2✅ L3✅ | Мониторинг RMSE/edge; уже в карточках | 3 ✅ |
| **GAME_5M entry CatBoost** | AUC≈0.50, gate ❌ | Analyzer backtest фаза C | 3 (заблокирован) |
| **Multiday LR gates** | REG on, entry `apply` | Сверка с log_only планом; hold `log_only` | 3 |
| **L3 resolve на prod** | mirror | 3–7 дней `resolve_divergence=0` → enable | 3 |
| **Recovery D4b** | Модель AUC≈0.71, log-only | go/no-go по D4a stats | 3 |
| **Event-reaction в торговле** | Train есть, gate RMSE | advisory only; hard-block после backtest | 3 |
| **Prod config audit** | Локальный example OK | Audit боевого `/app/config.env` | 4 |

### Prod ML full train (2026-06-05)

Коммиты: `6607dad` (L1 consolidation), `730db19` (archive), `8a03ea9` (recovery `--jsonl`). Все 8 контуров: `train_ran=True`. CatBoost: entry AUC≈0.50, portfolio RMSE≈0.078, recovery AUC≈0.71, event RMSE≈0.13. Multiday: **927** ridge JSON (merged universe).

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

# Ближайший план консолидации (2026-06-09 → 2026-07)

**Контекст:** [VERSION.md](../VERSION.md) v2.0.0, [ML_CONSOLIDATION_ROLLOUT_PLAN.md](ML_CONSOLIDATION_ROLLOUT_PLAN.md) фазы 0–3.  
**Git:** единственная рабочая ветка — `main`; расхождений remote-веток нет. «Консолидация ветвей» = **сведение документации, runtime-контуров и dual-track** в один канон, без параллельных «планов-истин».

---

## 1. Текущая точка (where we are)

```text
                    ┌─────────────────────────────────────┐
                    │  Канон: ML_AND_DECISION_ARCHITECTURE │
                    └─────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
   Legacy HOT PATH            Decision STACK              Earnings track
   (исполняет сделки)         (snapshot, RESOLVE=false)   (отдельная очередь)
          │                           │
   portfolio CatBoost L3      weekly mirror cron
   multiday entry apply        session veto = норма
   gap: naive PM baseline      projected_effective logged
   gap ML: advisory only
```

| Ветвь / контур | Зрелость | Блокер promotion |
|----------------|----------|------------------|
| **Portfolio CatBoost** | L3 prod | — (мониторинг RMSE) |
| **Multiday LR** | Entry apply; WF **ready** | Hold apply — ждём ≥5 would_defer |
| **Gap forecast** | Display ✅; pooled ridge trained | ML не beat naive PM на 90d OOS |
| **Entry CatBoost** | Train ✅ | AUC≈0.50, n_valid&lt;80 |
| **Recovery** | D4a telemetry | D4b go/no-go не закрыт |
| **Event-reaction** | Advisory + UI | RMSE gate; backtest с costs |
| **Decision RESOLVE** | false by policy | unexpected_divergence=0 — не включать |
| **Open-path** | Shadow | Prerequisites |

---

## 2. Спринт 3.2 (недели 1–2 после 2026-06-09)

| # | Задача | DoD | Владелец-контур |
|---|--------|-----|-----------------|
| 3.2.1 | **Gap pooled ridge в morning snapshots** | `game5m_gap_forecast_daily` с `pooled_gap_model.json` после premarket_cron; сверка pred/PM/fact 3 дня | gap_forecast |
| 3.2.2 | **Multiday hold telemetry** | Накопить would_defer; не включать hold `apply` | multiday_lr |
| 3.2.3 | **Закоммитить `scripts/run_multiday_wf_game5m.py`** | Cron или manual weekly → `last_multiday_wf_game5m.json` | multiday_lr / ops |
| 3.2.4 | **Prod config audit** | Diff `/app/config.env` vs `config.env.example` + [CLEANUP_CONFIG](archive/CLEANUP_CONFIG_AND_CODE_PLAN_2026-05-07.md) | фаза 4 |
| 3.2.5 | **Entry CatBoost** | Ждать n_valid≥80; без изменения gate | game5m_entry |
| 3.2.6 | **Earnings autoprep labels** | ≥40 LLM labels → grid refresh | earnings_grid |

**Не делать в 3.2:** `RESOLVE=true`, hold multiday `apply`, gap ML L3 promotion без beat baseline.

---

## 3. Спринт 3.3 (недели 3–4)

| # | Задача | Условие старта |
|---|--------|----------------|
| 3.3.1 | Пересмотр gap: pooled vs naive rolling 30d | 3.2.1 завершён |
| 3.3.2 | Event-reaction: trading backtest с transaction costs | RMSE стабилен после refresh |
| 3.3.3 | Recovery D4b decision | ≥20 новых TE rows |
| 3.3.4 | `POST /api/ml/refresh?contour=` (фаза 4) | После config audit |

---

## 4. Консолидация документации (параллельно)

| Действие | Статус 2026-06-09 |
|----------|-------------------|
| Канон ML + GAME_5M decision | ✅ |
| `PROJECT_STATUS` + `ML_STATUS_REPORT` живые | ✅ обновлены |
| Dated планы → `docs/archive/` + stub | ✅ ROADMAP, BUY/LLM, features, ARCH_OPT |
| Feature deep-dives с баннером *не канон матрицы* | ✅ DECISION_STACK, OPEN_PATH, multiday gates |
| `BUSINESS_PROCESSES` §0 статус | ✅ |
| Earnings dated plans в `earnings-event-agent-lse/` | Оставить; канон — IMPLEMENTATION_PLAN |

**Правило:** новый статус — только в `PROJECT_STATUS`, `ML_STATUS_REPORT`, `VERSION`; не плодить dated `*_PLAN_YYYY-MM-DD.md` в корне `docs/`.

---

## 5. Консолидация кода (архив)

| Путь | Содержание |
|------|------------|
| `scripts/archive/incidents/` | Разовые фиксы сделок |
| `scripts/archive/ml/` | Устаревшие train-скрипты до dispatcher |
| `scripts/run_multiday_wf_game5m.py` | OOS probe (из `_probe_*`) — в main, не в archive |

Stub-скрипты в корне `scripts/` → см. [scripts/archive/README.md](../scripts/archive/README.md).

---

## 6. Критерии «консолидация завершена» (v2.1 target)

| Метрика | Сейчас | Цель v2.1 |
|---------|--------|-----------|
| Dated планы без stub в `docs/` | 0 | 0 |
| Gap ML beat naive 30d rolling | ❌ | Пересмотр или явный defer |
| Multiday hold decision | log_only | Документированный go/no-go |
| `DECISION_STACK_RESOLVE` | false | false (пока unexpected=0) |
| Prod config audit | partial | ✅ signed-off |
| Earnings grid labels | 33/40 | ≥40 |

---

## 7. Порядок PR (рекомендуемый)

1. **PR-Docs-2.0** — VERSION, CONSOLIDATION_NEXT, PROJECT_STATUS, archive moves (без cron)
2. **PR-Gap-monitor** — после 3.2.1: метрики pooled vs PM в data-quality
3. **PR-Multiday-wf-cron** — weekly `run_multiday_wf_game5m.py` (опционально вс 07:00 UTC)
4. **PR-Config-audit** — только после согласования diff prod

Не смешивать docs PR с изменением `config.env` на prod.

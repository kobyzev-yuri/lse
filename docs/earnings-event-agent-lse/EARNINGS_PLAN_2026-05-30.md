# Earnings Intelligence — план на 2026-05-30

**Deploy:** `git push origin main` → `ssh ai8049520@104.154.205.58 "cd /home/ai8049520/lse && ./scripts/deploy_from_github.sh"`.

**Вчера:** [EARNINGS_PLAN_2026-05-29.md](./EARNINGS_PLAN_2026-05-29.md) (UI/ML вечер `6b17e91`…`4a144cf`).

---

## Цель дня

1. **Prod parity** — задеплоить UI с контекст-баром и прогнать META/MSFT end-to-end на `/earnings`.
2. **Данные** — убедиться, что `quotes_regime_earnings_v1` backfill на prod не отстаёт от regime_v1 (cron `23:37` или разовый `--include-earnings-universe`).
3. **P1 накопление** — больше LLM scenario labels и shadow/class метрик по мере созревания 5d.

---

## P0 — deploy + smoke

| # | Задача | Критерий | Статус |
|---|--------|----------|--------|
| 1 | Deploy `main` на GCP | `/earnings` открывается, sticky **Контекст** | ✅ `0a243f2` deploy 12:24 UTC; HTML: `eiContextBar`, `ctxSymbol`, `sessionStorage` |
| 2 | Smoke META + MSFT | Brief quotes; Fusion без `feature_version_mismatch`; Spillover от **report date** | ✅ Brief ok (META capex, MSFT gap_up); source 5d −9.89% / −4.16%; Fusion scenario ok, mismatch None; spillover row 2026-04-29 |
| 3 | Telegram `/earnings` | Тот же brief, что в UI | ✅ `format_brief_telegram`: scenario + tone + source fwd 1d/5d |

---

## P1 — данные и ML

| # | Задача | Критерий | Статус |
|---|--------|----------|--------|
| 4 | Counts `quotes_regime_earnings_v1` vs ERD | earnings_v1 не сильно ниже total | ✅ total **527**, earnings_v1 **482** (91.5%); gap **45** = `no_quotes` / `no_as_of_before_event` |
| 5 | Разовый backfill | `--include-earnings-universe` | ✅ +87 rows pass (12:30); full refresh backfill 482/527 (13:09) |
| 6 | `apply_earnings_scenario_labels --universe` | +labels | ⚠️ updated=0 (все 23 уже applied); новых hints нет без extract |
| 7 | `run_earnings_ml_refresh` full | readiness + shadow | ✅ train n=21, 4 classes; shadow **n=33**, sign **72.7%**, class **100%** (мало scored), gate ready; `overall_grid_ready=true` |
| 8 | Classifier train rows | накапливать labels | 🔄 n_train=21, n_valid=0 (holdout skipped); цель ≥50 для stable valid |

**Fusion после refresh:** META reg pred **+0.0019** log, scenario **capex** proba ~0.94.

---

## P2 — UX (по желанию)

| # | Задача | Примечание |
|---|--------|------------|
| 9 | Fusion: регрессия в **%** + bias labels | Не делали сегодня |
| 10 | Таблица events: фильтр по ticker / дате | Частично: чекбокс «только этот тикер» в **Контекст** (`4a144cf`) |

---

## Ops / cron (напоминание)

| Время | Скрипт |
|-------|--------|
| `23:36` пн–пт | `backfill_event_reaction_labeling.py` — `quotes_regime_v1`, `--include-all-symbols` |
| `23:37` пн–пт | то же — `quotes_regime_earnings_v1`, `--include-earnings-universe` |
| `23:52` пн–пт | `run_earnings_ml_refresh.py` — full train scenario |

---

## Acceptance

- [x] Prod = `main` после deploy (`0a243f2`)
- [x] META/MSFT: Brief + Fusion + Spillover без регрессий
- [x] `earnings_v1` feature rows в норме (482/527, gap = no quotes)
- [x] Обновлён этот файл по факту дня 30.05

---

## Следующие шаги

1. Extract + labels для событий без `scenario_hints` (рост llm_labels > 23).
2. Seed quotes для 45 строк `features:no_quotes`.
3. Spillover peers=0 на META/MSFT в API — проверить `peer_graph_edge` + outcomes для affected tickers.
4. Док: [EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](./EARNINGS_LLM_ML_LABELS_AND_TRAINING.md) — LLM vs ML, фазы prod.
5. **Полный prod:** [EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md) · sprint [EARNINGS_PLAN_2026-05-31.md](./EARNINGS_PLAN_2026-05-31.md).

---

## Ссылки

- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md)
- [EARNINGS_UI_GUIDE.md](./EARNINGS_UI_GUIDE.md)
- [EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](./EARNINGS_LLM_ML_LABELS_AND_TRAINING.md)

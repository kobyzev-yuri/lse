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

| # | Задача | Критерий |
|---|--------|----------|
| 1 | Deploy `main` на GCP | `/earnings` открывается, sticky **Контекст** сохраняет ticker/date между вкладками |
| 2 | Smoke META + MSFT | Brief quotes; Fusion без `feature_version_mismatch`; Spillover 1d/5d от **report date** |
| 3 | Telegram `/earnings` | Тот же brief, что в UI для выбранной даты |

---

## P1 — данные и ML

| # | Задача | Критерий |
|---|--------|----------|
| 4 | Проверить counts `quotes_regime_earnings_v1` vs `quotes_regime_v1` в ERD | ML layers tab / SQL; цель — earnings_v1 не сильно ниже regime для universe |
| 5 | При необходимости разовый backfill | `backfill_event_reaction_labeling.py --only-features --force-features --include-earnings-universe` в `lse-bot` |
| 6 | `apply_earnings_scenario_labels --universe` | +2–5 новых `llm_scenario_v0` labels; пересмотреть junk materials (ARM и т.п.) |
| 7 | `run_earnings_ml_refresh` (full или dry-run) | readiness JSON; shadow `n_matured` / sign% / class% в UI |
| 8 | Classifier train rows | Сейчас ~21 train rows, `n_valid` может быть 0 — накапливать labels до стабильного holdout |

---

## P2 — UX (по желанию)

| # | Задача | Примечание |
|---|--------|------------|
| 9 | Fusion: показывать регрессию в **%** + bias labels | Читаемость vs сырые log-ret |
| 10 | Таблица events: фильтр по ticker / дате | Согласовать с **Контекст** bar |

---

## Ops / cron (напоминание)

| Время | Скрипт |
|-------|--------|
| `23:36` пн–пт | `backfill_event_reaction_labeling.py` — `quotes_regime_v1` |
| `23:37` пн–пт | то же — `quotes_regime_earnings_v1`, `--include-earnings-universe` |
| `23:52` пн–пт | `run_earnings_ml_refresh.py` — full train scenario |

---

## Acceptance

- [ ] Prod = `main` после deploy
- [ ] META/MSFT: Brief + Fusion + Spillover без регрессий
- [ ] `earnings_v1` feature rows в норме для intelligence universe
- [ ] Обновить этот файл по факту вечера 30.05

---

## Ссылки

- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md)
- [EARNINGS_UI_GUIDE.md](./EARNINGS_UI_GUIDE.md)

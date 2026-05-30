# Earnings Intelligence — план на 2026-05-31 (Phase B sprint)

**Roadmap:** [EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md)  
**Deploy:** push → `deploy_from_github.sh`

---

## Цель дня

Закрыть **Phase B** backlog P0: больше LLM labels, quotes для ERD gaps, Fusion UX. Каждый шаг — **smoke после выполнения**.

---

## P0 — данные

| # | Задача | Команда (prod `lse-bot`) | Smoke / критерий |
|---|--------|--------------------------|------------------|
| 1 | Extract по events без hints | `extract_earnings_material_facts.py --since 2026-01-01 --limit 20` | SQL: ↑ `events_with_scenario_hints` |
| 2 | Apply labels | `apply_earnings_scenario_labels.py --universe` | **updated ≥ 5** OR llm_labels **≥ 28** |
| 3 | Seed quotes для no_quotes | `seed_quotes_for_event_reaction_dataset.py --include-all-dataset-symbols --min-quote-span-days 120` | gap **45 → &lt;20** |
| 4 | Backfill earnings_v1 | `backfill ... --include-earnings-universe --only-features --force-features --limit 600` | ev1 **≥ 500/527** |

---

## P1 — ML

| # | Задача | Smoke |
|---|--------|-------|
| 5 | `ML_READINESS_TRAIN_MODE=full run_earnings_ml_refresh.py` | n_train ↑; shadow n_matured ≥ 33 |
| 6 | META/MSFT Fusion + Brief | mismatch None; spillover peers ok ≥ 5 |

---

## P2 — UX (если P0 успел)

| # | Задача | Критерий |
|---|--------|----------|
| 7 | Fusion: reg **%** + bias в UI | deploy + visual check |
| 8 | Обновить roadmap/plan acceptance | commit docs |

---

## Smoke script (META 2026-04-29)

```python
# docker exec lse-bot python3 -c "..."  — см. EARNINGS_PRODUCT_ROADMAP.md §6
```

---

## Acceptance

- [ ] llm_scenario_labels ≥ 28 (interim) / ≥ 40 (stretch)
- [ ] earnings_v1 ≥ 500
- [ ] Extract + labels pipeline без ручного prod_eval
- [ ] Fusion % в UI (optional P2)

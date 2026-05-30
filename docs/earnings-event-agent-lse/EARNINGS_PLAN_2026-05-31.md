# Earnings Intelligence — план на 2026-05-31 (Phase B sprint)

**Roadmap:** [EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md)  
**Deploy:** push → `deploy_from_github.sh`  
**Peer graph (справка):** [PEER_GRAPH_PRINCIPLES.md](./PEER_GRAPH_PRINCIPLES.md)

---

## Цель дня

Закрыть **Phase B** backlog P0: больше LLM labels, quotes для ERD gaps, Fusion UX. Каждый шаг — **smoke после выполнения**.

---

## P0 — данные

| # | Задача | Команда (prod `lse-bot`) | Smoke / критерий | Статус |
|---|--------|--------------------------|------------------|--------|
| 1 | Extract по events без hints | `extract_earnings_material_facts.py --since 2026-01-01 --limit 20` | SQL: ↑ `events_with_scenario_hints` | ⚠️ все 20 **skip** (extraction_meta уже есть); hints **31** без роста |
| 2 | Apply labels | `apply_earnings_scenario_labels.py --universe` | **updated ≥ 5** OR llm_labels **≥ 28** | ⚠️ updated=0, llm **23** |
| 3 | Seed quotes для no_quotes | `seed_quotes ...` / `update_all_prices NBIS` | gap **45 → <20** | ⚠️ NBIS **398 rows** (2024-10→); gap **33** = события **2014–2022** (нет yfinance) |
| 4 | Backfill earnings_v1 | `EVENT_REACTION_FEATURE_BUILDER_VERSION=quotes_regime_earnings_v1 backfill ... --include-earnings-universe` | ev1 **≥ 500/527** | ⚠️ **494/527** (+12 от 482); **33** no_quotes |

**Ops:** backfill без env перезаписывает `quotes_regime_v1` — earnings_v1 восстановлен повторным прогоном с env (17:17 UTC).

---

## P1 — ML

| # | Задача | Smoke | Статус |
|---|--------|-------|--------|
| 5 | `ML_READINESS_TRAIN_MODE=full run_earnings_ml_refresh.py` | n_train ↑; shadow n_matured ≥ 33 | ✅ full refresh ~15:59 UTC; n_train **17**; shadow **n=33**, sign **60.6%** |
| 6 | META/MSFT Fusion + Brief | mismatch None; spillover peers ok ≥ 5 | ✅ META 10/12, MSFT 8/10 |

---

## P2 — UX (если P0 успел)

| # | Задача | Критерий | Статус |
|---|--------|----------|--------|
| 7 | Fusion: reg **%** + bias в UI | deploy + visual check | ✅ deploy; `fmtPct` в template; Fusion API ok |
| 8 | Обновить roadmap/plan acceptance | commit docs | ✅ этот файл |

---

## Итоги выполнения (2026-05-30, prod)

### Метрики snapshot

| Метрика | До sprint | После | Цель |
|---------|-----------|-------|------|
| ERD (universe) | 527 | **527** | — |
| earnings_v1 | 482 | **494** | ≥ 500 |
| llm_scenario_labels | 23 | **23** | ≥ 28 |
| events_with_scenario_hints | 31 | **31** | ↑ |
| feature gap | 45 | **33** | < 20 |
| peer_graph_edge | 96 | **96** | — |

### ML / gates

| | Значение |
|--|----------|
| `overall_grid_ready` | **true** |
| scenario n_train | **17** (4 classes) |
| shadow n_sign_scored | **33** |
| shadow sign_accuracy | **60.6%** |
| shadow class_accuracy | **90.5%** (scored subset) |
| regression | no_dataset (отдельный контур) |

### Smoke META/MSFT 2026-04-29

| | META | MSFT |
|--|------|------|
| Brief | ok (capex) | ok (gap_up) |
| Fusion mismatch | None | None |
| Spillover peers ok | **10/12** | **8/10** |

### Gap 33 — NBIS (структурный лимит)

| | |
|--|--|
| **Причина** | 33 ERD-строки NBIS с `event_time_et` **2014–2022**; котировки в БД только с **2024-10-25** |
| **ev1 по NBIS** | **7/40** (события 2025+) |
| **Seed** | 398 quotes обновлено — **не закрывает** старые события |
| **Макс ev1** | **494/527** (100% достижимых строк); цель **≥500/527** недостижима без prune skeleton или исторических quotes |

**Рекомендация:** исключить pre-2024 NBIS skeleton из `event_reaction_dataset` (`--kb-since` / cleanup) или принять метрику **494/494 quote-eligible**.

### Hints vs labels (узкое место)

**31** events с join hints↔ERD, но только **23** `llm_scenario_v0`. **8 строк** без LLM label:

| Причина | Символы (пример) | Что нужно |
|---------|-------------------|-----------|
| `auto_quotes_v1` (UP/DOWN) | AMD, AMZN, ANET, LITE, CIEN, NBIS | hints пустые в detail; re-extract не идёт из‑за `extraction_meta` |
| `kb_skeleton` без hints | DELL 2026-05-28, GOOGL 2026-02-04 | materials/extract или skeleton→LLM |

Extract `--limit 20` (16:49 UTC): **0 новых** LLM вызовов. Apply labels: **updated=0, skipped=31**.

---

## Acceptance

- [ ] llm_scenario_labels ≥ 28 (interim) / ≥ 40 (stretch) — **23**
- [ ] earnings_v1 ≥ 500 — **494** (макс **494** без prune NBIS skeleton)
- [ ] Extract + labels pipeline без ручного prod_eval — **extract исчерпан; нужен force-reextract / новые materials**
- [x] ML refresh full + shadow n≥33 — **PASS**
- [x] META/MSFT smoke — **PASS**
- [x] Fusion % в UI — **deploy ok** (`fmtPct`, bias labels)

**Вердикт sprint:** pipeline и ML **стабильны**; **acceptance по данным не закрыт**. Peer spillover ML — **после** закрытия Phase B (см. roadmap WS6).

---

## Следующие шаги

1. **NBIS skeleton:** prune 33 ERD rows (event &lt; 2024-10-25) или `EVENT_REACTION_KB_SINCE` — закрыть метрику ev1 coverage.
2. **llm labels 23→28:** force-reextract 8 событий (код `--force-reextract` или новые materials).
3. После sign-off Phase B → **peer spillover ML** dataset.

---

## Smoke script (META 2026-04-29)

```python
# docker exec lse-bot python3 -c "..."  — см. EARNINGS_PRODUCT_ROADMAP.md §6
```

---

## Ссылки

- [EARNINGS_PLAN_2026-05-30.md](./EARNINGS_PLAN_2026-05-30.md)
- [EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](./EARNINGS_LLM_ML_LABELS_AND_TRAINING.md)
- [PEER_GRAPH_PRINCIPLES.md](./PEER_GRAPH_PRINCIPLES.md)

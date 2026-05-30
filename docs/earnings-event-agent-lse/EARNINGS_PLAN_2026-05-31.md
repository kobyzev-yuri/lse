# Earnings Intelligence — план на 2026-05-31 (Phase B sprint)

**Roadmap:** [EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md)  
**Deploy:** `25ee774`…`44c7b88` → `deploy_from_github.sh`  
**Peer graph:** [PEER_GRAPH_PRINCIPLES.md](./PEER_GRAPH_PRINCIPLES.md)  
**Следующий:** [EARNINGS_PLAN_2026-06-01.md](./EARNINGS_PLAN_2026-06-01.md) (Phase C)

---

## Цель дня

Закрыть **Phase B** backlog: quotes/ev1 coverage, LLM labels, Fusion UX, ML refresh. **Smoke после каждого шага.**

---

## P0 — данные

| # | Задача | Smoke / критерий | Статус |
|---|--------|------------------|--------|
| 1 | Extract | hints ↑ | ⚠️ skip (extraction_meta); force-reextract ×8 ok |
| 2 | Apply labels | llm ≥28 | ⚠️ **24** (+1 LITE); 7 events — пустые hints (нет transcript) |
| 3 | Seed quotes / NBIS | gap ↓ | ✅ NBIS 398 quotes; prune 33 skeleton |
| 4 | Backfill ev1 | ev1 coverage | ✅ **494/494** (100%) после prune |

**Ops:** backfill earnings_v1 только с `EVENT_REACTION_FEATURE_BUILDER_VERSION=quotes_regime_earnings_v1`.

---

## P1 — ML

| # | Задача | Smoke | Статус |
|---|--------|-------|--------|
| 5 | `run_earnings_ml_refresh.py --full` | dry_run=False; shadow n≥33 | ✅ **19:14 UTC** `--full`; n_train **24**, valid **83.3%**, shadow **33/60.6%** |
| 6 | META/MSFT | mismatch None; spillover ≥5 | ✅ META 10/12, MSFT 8/10 |

---

## P2 — UX

| # | Задача | Статус |
|---|--------|--------|
| 7 | Fusion reg **%** + bias | ✅ `fmtPct` на prod |
| 8 | Документация | ✅ этот файл + PEER_GRAPH_PRINCIPLES |

---

## Финальные метрики prod (2026-05-30)

| Метрика | Старт | Финал | Цель | |
|---------|-------|-------|------|---|
| ERD | 527 | **494** | quote-eligible | ✅ prune NBIS |
| earnings_v1 | 482 | **494/494** | coverage 100% | ✅ |
| llm_scenario_labels | 23 | **24** | ≥28 interim | ⚠️ |
| peer_graph_edge | 96 | **96** | — | ✅ |

### ML / gates (19:14 UTC, `--full`)

| | |
|--|--|
| `overall_grid_ready` | **true** |
| n_train / valid_acc | **24** / **83.3%** (5 classes) |
| shadow n / sign | **33** / **60.6%** |
| ev1 | **494/494** |

### Выполненные ops

| Время (UTC) | Действие |
|-------------|----------|
| 17:59 | deploy `25ee774` (prune script, force-reextract) |
| 17:59:46 | prune NBIS **33** rows (`before 2024-10-25`) |
| 18:03–18:05 | force-reextract ×8 (LLM ok) |
| 18:05 | apply labels +1 (LITE) |
| 18:58 | deploy `44c7b88` (`--full` flag) |
| 19:14 | **`run_earnings_ml_refresh.py --full`** — train n=24, valid 83.3%, shadow ok |

---

## Acceptance Phase B

- [x] **ev1 coverage** — 494/494 (100% quote-eligible)
- [x] **NBIS prune** — 33 skeleton удалено (`prune_event_reaction_dataset.py`)
- [x] **ML pipeline + smoke** — grid ready, META/MSFT ok
- [x] **Fusion % UI** — deploy ok
- [ ] **llm ≥28** — **24** (interim: нужны transcript для 7 events)
- [x] **force-reextract** — код + прогон на prod

**Вердикт:** Phase B **закрыт по ev1/ML/UI**; llm labels — **interim 24/28**. Переход → [Phase C peer spillover ML](./EARNINGS_PLAN_2026-06-01.md).

---

## Smoke script

```bash
docker exec lse-bot python3 -c "
import json, urllib.request
BASE='http://127.0.0.1:8080'
for sym in ('META','MSFT'):
  ed='2026-04-29'
  b=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/brief/{sym}?event_date={ed}',timeout=60).read())
  f=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/fusion/{sym}?event_date={ed}',timeout=60).read())
  print(sym, b.get('status'), f.get('feature_version_mismatch'))
"
```

---

## Ссылки

- [EARNINGS_PLAN_2026-05-30.md](./EARNINGS_PLAN_2026-05-30.md)
- [EARNINGS_LLM_ML_LABELS_AND_TRAINING.md](./EARNINGS_LLM_ML_LABELS_AND_TRAINING.md)
- [PEER_GRAPH_PRINCIPLES.md](./PEER_GRAPH_PRINCIPLES.md)

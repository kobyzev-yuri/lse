# Earnings Intelligence — план на 2026-06-01 (Phase C старт)

**Roadmap:** [EARNINGS_PRODUCT_ROADMAP.md](./EARNINGS_PRODUCT_ROADMAP.md) · Phase B закрыт: [EARNINGS_PLAN_2026-05-31.md](./EARNINGS_PLAN_2026-05-31.md)

---

## Цель

1. Зафиксировать **Phase B exit** (ev1 coverage, ML smoke, доки).
2. Начать **Phase C — peer spillover ML** (dataset + baseline predict).

---

## P0 — Phase B sign-off (smoke)

| # | Задача | Команда | Критерий | Статус |
|---|--------|---------|----------|--------|
| 1 | Full ML refresh | `run_earnings_ml_refresh.py --full` | log `dry_run=False`; n_train ≥18 | ✅ 19:14 UTC; n_train **24**, valid_acc **83.3%**, 5 classes |
| 2 | Smoke checklist | roadmap §6 | context bar, META/MSFT, readiness, shadow | ✅ context bar; META/MSFT ok; grid **true**; shadow **33/60.6%** |
| 3 | Обновить plan 31.05 | commit docs | acceptance финал | ✅ |

---

## P1 — Peer spillover ML (Phase C)

| # | Задача | Smoke | Статус |
|---|--------|-------|--------|
| 4 | Dataset `(source_event, peer) → peer_forward_log_ret_5d` | `build_peer_spillover_dataset.py --dry-run` | ✅ **162 rows**, 29 events, 14 peers |
| 5 | Baseline propagation score | `baseline_weighted_sign_acc` в summary JSON | ✅ **36.4%** baseline; ML valid **85.4%** |
| 6 | UI: LLM vs ML scenario + peer spillover ML | smoke `/earnings` Brief/Spillover/Fusion | ✅ `be72376` deploy 22:21 UTC; META 16 peer ML preds |

См. [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §5 peer spillover ML.

---

## P1.5 — Dataset gate fix (2026-05-30 вечер)

| # | Задача | Результат |
|---|--------|-----------|
| A | Разделить `hints_pending_apply` vs `events_missing_llm_extract` | commit `f48b676`; gate не блокировался на пустых `[]` hints |
| B | force-reextract 7 symbols + apply labels | +4 labels (ANET, PLTR, AMD, GOOGL); **25** LLM labels |
| C | Incremental ML refresh | grid **true**, scenario **true**, peer **true** (21:42 UTC) |

**Readiness snapshot (prod):**

| Gate | Значение |
|------|----------|
| `overall_grid_ready` | **true** |
| `overall_scenario_classifier_ready` | **true** |
| `overall_peer_spillover_ready` | **true** |
| LLM labels | **25** / 5 classes |
| Classifier valid_acc | **42.9%** (n_valid=7; holdout появился) |
| Peer spillover sign_acc | **85.4%** vs baseline **31.7%** |
| Shadow | n=33, sign **60.6%** |

**Остаток датасета (не блокирует gate):**

- `symbols_without_labels`: CIEN, DELL, NBIS
- `events_missing_llm_extract`: 6 (auto_quotes / kb_skeleton, hints=0)
- sparse classes: `miss_or_guide_breakdown`, `beat_revaluation_down` (по 1 sample)

---

## P2 — llm labels (backlog)

| # | Задача | Примечание | Статус |
|---|--------|------------|--------|
| 7 | Ingest transcript для 7 events | DELL, GOOGL, AMD, AMZN, ANET, CIEN, NBIS | 🔄 **25/28** labeled; materials sync cron |
| 8 | force-reextract + apply | после materials | ✅ batch 30.05; cron extract `:25 */6` |

---

## Smoke (META 2026-04-29)

```bash
docker exec lse-bot python3 -c "
import json, urllib.request
BASE='http://127.0.0.1:8080'
sym, ed = 'META', '2026-04-29'
b=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/brief/{sym}?event_date={ed}',timeout=60).read())
f=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/fusion/{sym}?event_date={ed}',timeout=60).read())
s=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/spillover/{sym}?limit=1',timeout=60).read())
ev=(s.get('events') or [{}])[0]
sm=b.get('scenario_ml') or {}
ml=[x for x in (b.get('peer_spillover_ml') or []) if x.get('peer_spillover_ml_status')=='ok']
print('brief', b.get('status'), 'scenario_ml', sm.get('predicted_scenario'), 'peer_ml_ok', len(ml))
print('fusion peer_ml', len(f.get('peer_spillover_ml') or []))
print('spillover scenario_ml', (ev.get('scenario_ml') or {}).get('predicted_scenario'))
"
```

Последний smoke **30.05 22:21 UTC** (`be72376`): META brief **ok**, scenario_ml `capex_positive_for_infra_peers`, **16/16** peer_spillover_ml ok; fusion + spillover API с `peer_spillover_ml` / `scenario_ml`.

---

## Acceptance Phase C (interim)

- [x] Peer spillover dataset rows ≥ 100 (source×peer×event) — **162 rows**, 29 events, 14 peers (2026-05-30)
- [x] Baseline weighted spillover sign accuracy documented — **36.4%** same-sign; ML valid **85.4%** vs baseline **31.7%**
- [x] Phase B doc signed off in EARNINGS_PLAN_2026-05-31.md
- [x] Analyzer readiness gates green (grid + scenario + peer)
- [x] UI: LLM vs ML scenario + peer spillover ML pred в Brief / Spillover / Fusion (`be72376`)

---

## План на 2026-05-31 (завтра)

**Prod:** `be72376` · gates green · Phase C ML **закрыт** · Phase C **product** — следующий блок.

| P | Задача | Критерий |
|---|--------|----------|
| **P0** | Накопление labels | cron extract/apply; CIEN/DELL/NBIS — хотя бы +1 label или materials |
| **P0** | Shadow ≥50 matured | `n_matured` в shadow-report (сейчас 33) |
| **P1** | Telegram alert после отчёта | Brief link + LLM/ML scenario + top 3 peers (roadmap C) |
| **P1** | Runbook partial brief / no materials | md в `docs/earnings-event-agent-lse/` |
| **P1** | Weekly prod_eval в cron/runbook | `run_earnings_intelligence_prod_eval` |
| **P2** | Materials junk audit (ARM, bare PDF) | roadmap B8 |
| **P2** | Weighted spillover validation metric | roadmap B9 |
| **—** | Phase D | **не начинать** без C sign-off + backtest |

**Smoke утром:** `/earnings` META 2026-04-29 → Brief (LLM/ML compare + peer ML cols) · Fusion · Spillover context block · analyzer gates.

---

## Следующие шаги (Phase C product → D)

1. ~~**UI ML spillover + classifier**~~ — ✅ `be72376` (2026-05-30 22:21 UTC).
2. **Накопление labels** — CIEN/DELL/NBIS + shadow **≥50** matured (roadmap Phase B targets).
3. **Phase C product** (roadmap §Phase C): Telegram alert после отчёта, runbook partial brief, weekly prod_eval.
4. **Phase D** — только после C + backtest: `event_fusion_policy.py`, shadow walk-forward.

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
| 6 | UI: LLM vs ML scenario + peer spillover ML | smoke `/earnings` Brief/Spillover/Fusion | ✅ `be72376` → `8f4834d` spillover UX |

---

## P1.6 — Spillover ML UX (2026-05-30 поздно)

| # | Fix | Commit |
|---|-----|--------|
| A | ML pred для LLM `affected` (GOOGL на MSFT) | `8f4834d` |
| B | Не дублировать Контекст в истории spillover | `8f4834d` |
| C | Исключить **source** из affected peers (ALAB→ALAB) | *(этот commit)* |
| D | Таблица: graph + affected **с fact**; aff без quotes скрыты | *(этот commit)* |
| E | Подпись pilot: pred ≈0, Sign ≠ KPI, advisory only | *(этот commit)* |

**Наблюдение (ALAB):** holdout sign acc 85% — среднее; на event pred сжимаются к ~0; `affected_only` давали одинаковый pred — не для train/sizing до B9 + больше rows.

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

**Prod после push:** spillover pilot UX + self-peer fix · gates green · Phase C ML **закрыт**.

**Open-path MVP (после autoprep):** [OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md](../OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md) — analyzer gates `overall_earnings_autoprep_ready` / `overall_open_path_mvp_prerequisites_ready`.

### Утро — smoke

```bash
# ALAB: нет строки ALAB в peers; aff без quotes скрыты
# MSFT 2026-04-29: GOOGL aff с fact; один блок Контекст
docker exec lse-bot python3 -c "
import json, urllib.request
b=json.loads(urllib.request.urlopen('http://127.0.0.1:8080/api/earnings/brief/ALAB?event_date=2026-02-10',timeout=60).read())
tickers=[p.get('ticker') for p in b.get('peer_spillover_outcomes') or []]
assert 'ALAB' not in tickers, tickers
print('ALAB peers ok', tickers)
"
```

`/earnings` → Spillover ALAB · Brief META/MSFT · analyzer gates.

### Приоритеты

| P | Задача | Критерий |
|---|--------|----------|
| **P0** | Labels backlog | CIEN/DELL/NBIS + cron extract/apply; цель **28/28** или +materials |
| **P0** | Shadow ≥50 matured | `n_matured` в shadow-report (сейчас 33) |
| **P1** | Runbook earnings desk | partial brief, no materials, **ML spillover pilot** (pred≈0, Sign sanity), failure modes (MSFT source −4% при gap_up) |
| **P1** | Telegram alert после отчёта | Brief link + LLM/ML scenario + top 3 graph peers |
| **P1** | Weekly prod_eval | cron/runbook `run_earnings_intelligence_prod_eval` |
| **P2** | Weighted spillover metric (B9) | validation до обсуждения качества ML |
| **P2** | Materials junk audit (B8) | ARM, bare PDF |
| **—** | Phase D | **не начинать** |

### Не делать завтра

- Не подключать spillover/classifier к GAME_5M (Phase D).
- Не переобучать spillover «ради UI» — сначала labels + rows.

---

## Следующие шаги (Phase C product → D)

1. ~~**UI ML spillover + classifier**~~ — ✅ `be72376` … spillover pilot UX (2026-05-30).
2. **Runbook + labels + shadow≥50** — Phase B maturity + Phase C product ops.
3. **Phase C product:** Telegram alert, weekly prod_eval.
4. **Phase D** — только после C + backtest.

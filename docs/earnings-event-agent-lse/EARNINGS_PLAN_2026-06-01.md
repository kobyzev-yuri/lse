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

| # | Задача | Smoke |
|---|--------|-------|
| 4 | Dataset `(source_event, peer) → peer_forward_log_ret_5d` | SQL row count ≥ N |
| 5 | Baseline propagation score | shadow peer sign vs actual |
| 6 | Fusion/Brief: optional pred peer column | API field smoke |

См. [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §5 peer spillover ML.

---

## P2 — llm labels (backlog)

| # | Задача | Примечание |
|---|--------|------------|
| 7 | Ingest transcript для 7 events | DELL, GOOGL, AMD, AMZN, ANET, CIEN, NBIS — сейчас **24/28** |
| 8 | force-reextract + apply | после materials |

---

## Smoke (META 2026-04-29)

```bash
docker exec lse-bot python3 -c "
import json, urllib.request
BASE='http://127.0.0.1:8080'
for sym in ('META','MSFT'):
  ed='2026-04-29'
  b=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/brief/{sym}?event_date={ed}',timeout=60).read())
  f=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/fusion/{sym}?event_date={ed}',timeout=60).read())
  s=json.loads(urllib.request.urlopen(f'{BASE}/api/earnings/spillover/{sym}?limit=1',timeout=60).read())
  po=(s.get('events') or [{}])[0].get('peer_outcomes') or []
  ok=sum(1 for p in po if p.get('status')=='ok')
  print(sym,'brief',b.get('status'),'fusion',f.get('feature_version_mismatch'),'spillover',ok,len(po))
"
```

---

## Acceptance Phase C (interim)

- [ ] Peer spillover dataset rows ≥ 100 (source×peer×event)
- [ ] Baseline weighted spillover sign accuracy documented
- [ ] Phase B doc signed off in EARNINGS_PLAN_2026-05-31.md

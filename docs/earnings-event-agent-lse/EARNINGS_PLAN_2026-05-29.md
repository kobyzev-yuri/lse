# Earnings Intelligence — план на 2026-05-29

**Deploy:** `git push origin main` → VM `./scripts/deploy_from_github.sh`.

**ML-слои (термины):** [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §7 · UI: `/earnings/guide`

---

## Цель дня

1. **P0 ✅** — честный prod UX на `/earnings` (Brief, shadow labels, spillover sources).
2. **P1** — ML layers tab + materials/extract; обновлять этот файл по факту тестов.

---

## Глоссарий (кратко)

| Термин | Что это | ML? |
|--------|---------|-----|
| **Event regression** | Pred **5d log-ret source** после earnings | ✅ CatBoost |
| **Scenario classifier** | Pred **тип сценария** (fade, contagion, capex peers…) | ✅ CatBoost |
| **Multiday ridge** | Drift **1–3d** на **любой** день (GAME_5M) | ✅ ridge, другой контур |
| **Spillover** | **Факт** 1d/5d peers от даты отчёта source | ❌ quotes history |
| **Shadow** | Качество classifier vs созревший 5d | ❌ offline report |
| **Fusion** | Regression + classifier + brief; `execution_blocked` | advisory bundle |
| **Peer graph weight** | Структурная гипотеза source→peer (0–1) | ❌ каталог v0 |

---

## Статус задач

### P0 — UI ✅ (deploy `fe26776` + `2b0664d`)

| # | Задача | Статус |
|---|--------|--------|
| 1–5 | Brief, shadow, graph, spillover | ✅ prod tested |

### P1 — код

| # | Задача | Статус |
|---|--------|--------|
| 9 | ML layers: Shadow + Fusion + readiness JSON | ✅ `0525849` prod |
| 10 | Docs: TRADE_ML §7 ridge vs regression vs classifier | ✅ `77cf513` |

### P1 — данные (ops)

| # | Задача | Статус |
|---|--------|--------|
| 6 | `run_earnings_intelligence_prod_eval.py --skip-ml-refresh` | ✅ 2026-05-29 15:22 UTC |
| 7 | DELL / свежие KB (materials + LLM) | ✅ prod brief ok |
| 8 | Fool 429 + ARM junk discover-links | ✅ `FOOL` cooldown + PDF filter |
| 11 | ANET/AVGO/GOOGL/PLTR coverage | ✅ ERD + materials + earnings_v1 backfill |
| 12 | ERD cron allowlist + backfill `--include-all-symbols` | ✅ `99e2715`, `339d17f` |
| 13 | **Train:** scenario classifier + regression refresh | ✅ deploy `838e9fa` 2026-05-29 |

**P1 #11–13:** ERD **527**, **482** `earnings_v1`, **23** LLM labels; scenario `.cbm` + regression RMSE≈0.141; shadow **n=33**. Deploy: `838e9fa`.

**P1 #6 итог:** sync 81 rows, ingest 0, extract 1 (ARM), shadow `n_matured=27` (не упал), readiness `overall_grid_ready=true`.

**P1 #7 fix (код):** `ensure_kb_and_link_orphan_materials` в sync — материалы с `event_date` без KB (DELL SEC `auto_sources`) получают anchor `knowledge_base` EARNINGS → extract → brief.

**P1 #8:** cron уже `--no-auto-fool`; код: cooldown файл при 429, пауза между probe, `_should_register_discovered_url` (bare PDF без transcript/earnings в path).

---

## Acceptance

- [x] P0 на prod
- [x] TRADE_ML_DATASETS обновлён (§4–§7)
- [x] ML layers tab: `live_shadow`, `fusion_advisory`, `readiness_gates` + json_path/metrics
- [x] `n_matured=27` shadow не упал после materials run
- [x] Prod eval JSON → `last_earnings_intelligence_prod_eval.json`
- [x] DELL: KB EARNINGS row + LLM extract → brief
- [x] Запись в `EARNINGS_INTELLIGENCE_PLAN.md` §2026-05-29

---

## Ссылки

- [EARNINGS_UI_GUIDE.md](./EARNINGS_UI_GUIDE.md)
- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md)
- Prod: `/app/logs/ml/ml_data_quality/last_earnings_intelligence_readiness.json`

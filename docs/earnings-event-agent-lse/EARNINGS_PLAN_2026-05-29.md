# Earnings Intelligence — план на 2026-05-29

**Deploy:** `git push origin main` → VM `./scripts/deploy_from_github.sh`.

**ML-слои (термины):** [TRADE_ML_DATASETS_AND_TARGETS_RU.md](../TRADE_ML_DATASETS_AND_TARGETS_RU.md) §7 · UI: `/earnings/guide`

---

## Цель дня

1. **P0 ✅** — честный prod UX на `/earnings` (Brief, shadow labels, spillover sources).
2. **P1 ✅** — ML layers, materials, train; вечером — UX/ML согласованность (даты, CatBoost FBV, вкладки).
3. **Док ✅** — этот файл + [EARNINGS_PLAN_2026-05-30.md](./EARNINGS_PLAN_2026-05-30.md).

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
| **Контекст** | Sticky bar ticker + event_date на всех вкладках | ❌ `sessionStorage` |

---

## Статус задач

### P0 — UI ✅ (deploy `fe26776` + `2b0664d` + вечер `6b17e91`…`4a144cf`)

| # | Задача | Статус |
|---|--------|--------|
| 1–5 | Brief, shadow, graph, spillover | ✅ prod tested |
| 14 | Кнопки Brief / Fusion / Spillover (event delegation) | ✅ `6b17e91` |
| 15 | Колонка сценария = top scenario (как Brief), не `hints[0]` | ✅ `6b17e91` |
| 16 | Evidence quotes `{topic, quote}` в UI и Telegram | ✅ `7f142b0` |
| 17 | Дата события = **report date** (`kb.ts::date DESC`), pickers + URL | ✅ `45f04d5` |
| 18 | Brief — отдельная вкладка; 1d/5d pills на Spillover | ✅ `15e3c96`, `90eaa8e` |
| 19 | Подсказки по вкладкам + легенды graph/shadow | ✅ `ff0b1dd` |
| 20 | Sticky **Контекст** (ticker/date) между вкладками | ✅ `4a144cf` |

### P1 — код / ML

| # | Задача | Статус |
|---|--------|--------|
| 9 | ML layers: Shadow + Fusion + readiness JSON | ✅ `0525849` prod |
| 10 | Docs: TRADE_ML §7 ridge vs regression vs classifier | ✅ `77cf513` |
| 21 | CatBoost `feature_version_mismatch` после nightly `quotes_regime_v1` | ✅ `ddbd5d3` — FBV preference, on-the-fly rebuild, fusion через `predict_event_reaction_for_ticker` |
| 22 | ML layers: текст регрессии (model FBV + row counts earnings_v1 vs regime_v1) | ✅ `d7730f2` |
| 23 | Cron `23:37` backfill `quotes_regime_earnings_v1` (`--include-earnings-universe`) | ✅ в `lse-docker.crontab` |

### P1 — данные (ops)

| # | Задача | Статус |
|---|--------|--------|
| 6 | `run_earnings_intelligence_prod_eval.py --skip-ml-refresh` | ✅ 2026-05-29 15:22 UTC |
| 7 | DELL / свежие KB (materials + LLM) | ✅ prod brief ok |
| 8 | Fool 429 + ARM junk discover-links | ✅ `FOOL` cooldown + PDF filter |
| 11 | ANET/AVGO/GOOGL/PLTR coverage | ✅ ERD + materials + earnings_v1 backfill |
| 12 | ERD cron allowlist + backfill `--include-earnings-universe` | ✅ `99e2715`, `339d17f` |
| 13 | **Train:** scenario classifier + regression refresh | ✅ deploy `838e9fa` |

**P1 #11–13:** ERD **527**, **482** `earnings_v1`, **23** LLM labels; scenario `.cbm` + regression RMSE≈0.141; shadow **n=33**, sign ~64%, class ~76%, gate ready.

**Валидация вечером:** META 2026-04-29 — Brief/Telegram (capex peers, quotes); Fusion — reg +0.0019 log (~+0.19% 5d), scenario capex proba ~0.94, conviction low (слабая регрессия + advisory rules).

**P1 #6 итог:** sync 81 rows, ingest 0, extract 1 (ARM), shadow `n_matured=27` (не упал), readiness `overall_grid_ready=true`.

**P1 #7 fix (код):** `ensure_kb_and_link_orphan_materials` в sync — материалы с `event_date` без KB (DELL SEC `auto_sources`) получают anchor `knowledge_base` EARNINGS → extract → brief.

**P1 #8:** cron уже `--no-auto-fool`; код: cooldown файл при 429, пауза между probe, `_should_register_discovered_url` (bare PDF без transcript/earnings в path).

---

## Acceptance (конец дня)

- [x] P0 на prod
- [x] TRADE_ML_DATASETS обновлён (§4–§7)
- [x] ML layers tab: `live_shadow`, `fusion_advisory`, `readiness_gates` + json_path/metrics
- [x] `n_matured` shadow не упал после materials run (→ **33** после train)
- [x] Prod eval JSON → `last_earnings_intelligence_prod_eval.json`
- [x] DELL: KB EARNINGS row + LLM extract → brief
- [x] Запись в `EARNINGS_INTELLIGENCE_PLAN.md` §2026-05-29
- [x] UI: вкладки Brief/Fusion/Spillover согласованы; даты отчёта; CatBoost mismatch исправлен
- [ ] Prod deploy последних UI-коммитов (`4a144cf`) — см. [план 30.05](./EARNINGS_PLAN_2026-05-30.md)

---

## Ссылки

- [EARNINGS_UI_GUIDE.md](./EARNINGS_UI_GUIDE.md)
- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md)
- **Следующий день:** [EARNINGS_PLAN_2026-05-30.md](./EARNINGS_PLAN_2026-05-30.md)
- Prod: `/app/logs/ml/ml_data_quality/last_earnings_intelligence_readiness.json`

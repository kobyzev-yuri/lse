# Earnings calendar — план срочной доработки (2026-06-16)

**Цель:** довести earnings pipeline до состояния «ждём только события календаря» — все cron/gates зелёные, ручные backfill не нужны.

**Prod snapshot (2026-06-16):**

| Метрика | Значение | Gate |
|---------|----------|------|
| `llm_scenario_labels` | 42 | ≥40 ✅ |
| `overall_grid_ready` / `peer` | ✅ | |
| `labeling_gaps` | 0 / 0 | ✅ |
| `shadow_n_matured` | 43 | need 50 ❌ |
| `overall_earnings_autoprep_ready` | ❌ | shadow + labels |
| ERD rows | 505 (504 outcomes) | |

---

## P0 — сегодня (код + deploy) ✅

| # | Задача | Файлы | Критерий готовности |
|---|--------|-------|---------------------|
| P0.1 | Date tolerance ±1d KB↔materials | `services/earnings_event_date_match.py`, calendar, extract, sync | ARM 2026-05-05 pending закрывается материалами 2026-05-06 |
| P0.2 | Past-only в pipeline autoprep | `earnings_calendar_new_events.py`, autoprep | Cron не тратит ingest/extract на future events |
| P0.3 | Ops backfill 9 past events | prod commands | labels ≥ 40 |

## P1 — сразу после P0 ✅

| # | Задача | Файлы | Критерий готовности |
|---|--------|-------|---------------------|
| P1.1 | Discover после failed ingest | `run_earnings_intelligence_autoprep.py` | autoprep log: `materials_discover` после ingest rc≠0 |
| P1.1b | Fix discover SQL (`ed.knowledge_base_id`) | `discover_earnings_material_sources.py` | discover не падает на prod |
| P1.2 | Extract: skip не в limit | `extract_earnings_material_facts.py` | `--all-events` не сжигает limit на already-extracted |
| P1.3 | Zip unpack (ASML) | `earnings_material_parser.py` | `application/zip` → parsed text |

## P2 — автоматизация данных (2026-06-16) ✅

| # | Задача | Файлы | Критерий |
|---|--------|-------|----------|
| P2.1 | Weekly seed quotes | `seed_quotes_for_event_reaction_dataset.py`, crontab Sun 05:25 | quotes для ERD universe |
| P2.2 | ERD past_only + dedup + prune | `build_event_reaction_dataset.py` | нет junk skeletons / false no_quotes |
| P2.3 | Labeling gaps alert | `services/erd_labeling_gaps.py`, cron 23:38 | Telegram при no_quotes / anchor_unresolved |
| P2.4 | Earnings universe в `update_prices` | `update_prices.py` | daily quotes покрывают universe |

## P3 — финальная автоматизация (текущий спринт)

| # | Задача | Файлы | Критерий |
|---|--------|-------|----------|
| P3.1 | Telegram при `overall_earnings_autoprep_ready` flip | `services/earnings_autoprep_gate_alert.py`, hook в `write_earnings_intelligence_readiness` | одно сообщение false→true; state JSON |
| P3.2 | Nightly `--force-outcomes` | crontab 23:34, `backfill_event_reaction_labeling.py` | shadow_n 43→50 по мере созревания 5d outcomes |
| P3.3 | ARM ingest URL blacklist | `services/earnings_material_ingest_skip.py`, `ingest_earnings_materials.py` | edge.media-server.com → `parse_status=skipped` |

### P3 deploy

1. Commit + `git push origin main`
2. `ssh gcp-lse "cd ~/lse && ./scripts/deploy_from_github.sh"`
3. Обновить crontab на VM: `crontab -l` → сверить с `crontab/lse-docker.crontab`
4. Однократно на prod (опционально, ускорить shadow gate):

```bash
docker exec lse-bot python scripts/backfill_event_reaction_labeling.py \
  --dataset-version v0_expanded_baseline --only-outcomes --force-outcomes \
  --include-earnings-universe --limit 800

docker exec lse-bot python scripts/ingest_earnings_materials.py \
  --status registered,failed --limit 30
# ARM edge.media-server rows → skipped
```

---

## P4 — после закрытия `overall_earnings_autoprep_ready` (ожидание ~3–7 дней)

Автоматически (cron, без ручных действий):

| Время | Job | Эффект |
|-------|-----|--------|
| nightly 23:33 | ERD build (`past_only`, prune) | новые KB events → skeleton |
| nightly 23:34 | force-outcomes | 5d returns → shadow maturation |
| nightly 23:36–37 | ERD label + earnings features | features актуальны |
| nightly 23:38 | labeling gaps audit | alert если quotes дырка |
| nightly 23:47 | ML refresh dispatcher | train + readiness JSON |
| nightly 23:50 | ml_train_readiness | gates + **autoprep Telegram flip** |
| autoprep cron | ingest/extract/labels past-only | новые past events из календаря |
| Sun 05:25 | seed_quotes weekly | quotes replenishment |

**Критерий P4:** `overall_earnings_autoprep_ready=true` (shadow_n≥50, labels≥40, grid+peer green).

---

## P5 — Phase C и open-path (после зелёного autoprep gate)

| # | Задача | Зависимость |
|---|--------|-------------|
| P5.1 | Phase C Telegram brief (earnings advisory) | `overall_earnings_autoprep_ready` |
| P5.2 | Open-path MVP prerequisites | см. `docs/OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md` |
| P5.3 | Fool 429 backoff / alternate transcript | снижение pending_extract на edge tickers |

Не блокируют переход в «calendar wait» — делаются параллельно или после flip-alert.

---

## P6 — состояние «ждём календарь» (целевой steady state)

**Что работает само:**

- Календарь earnings → KB → nightly ERD build подхватывает **past** events
- Autoprep cron: discover → ingest → extract → scenario labels (past-only)
- Quotes: `update_prices` + weekly seed
- ML: nightly full train + readiness gates
- Alerts: labeling gaps, autoprep gate flip, proxyapi balance

**Что делает оператор:** ничего, кроме мониторинга Telegram.

**Что ждём от календаря:**

- Новые earnings dates по universe (CIEN, ARM, ASML, …)
- После event date + 1d: materials pipeline
- После event + 5d: outcomes mature → shadow sample растёт
- Fool cooldown recovery для pending без SEC

**Периодический чек (раз в неделю, опционально):**

```bash
docker exec lse-bot cat /app/logs/ml/ml_data_quality/last_earnings_intelligence_readiness.json \
  | python3 -c "import json,sys; g=json.load(sys.stdin)['gates']; print(g)"
```

Ожидаем: все `overall_*` green, `labeling_gaps.ready=true`, `pending_extract` past-only ≤3.

---

## Deploy (общий)

1. Commit + `git push origin main`
2. `ssh gcp-lse "cd ~/lse && ./scripts/deploy_from_github.sh"`
3. Сверить crontab с репозиторием

### Исторический backfill (P0.3, выполнен)

```bash
docker exec lse-bot python scripts/run_earnings_intelligence_autoprep.py \
  --all-events --ingest-limit 80 --extract-limit 20

docker exec lse-bot python scripts/discover_earnings_material_sources.py \
  --since 2026-01-01 --symbols CIEN,ARM,ASML,NBIS,ALAB,SNDK --register

docker exec lse-bot python scripts/ingest_earnings_materials.py \
  --new-events-only --since 2026-01-01 --limit 60

docker exec lse-bot python scripts/extract_earnings_material_facts.py \
  --new-events-only --since 2026-01-01 --limit 15

docker exec lse-bot python scripts/apply_earnings_scenario_labels.py --universe
```

---

## Метрики успеха (финал)

- `llm_scenario_labels` ≥ 40 ✅
- `shadow_n_matured` ≥ 50 (cron force-outcomes)
- `overall_earnings_autoprep_ready` = true
- `labeling_gaps` = 0 / 0 ✅
- `pending_extract` past-only ≤ 3
- Telegram: одно сообщение «Earnings autoprep gate OPEN»
- Далее: passive calendar wait (P6)

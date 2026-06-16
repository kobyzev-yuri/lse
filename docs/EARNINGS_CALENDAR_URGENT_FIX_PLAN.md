# Earnings calendar — план срочной доработки (2026-06-16)

**Цель:** разблокировать materials → extract → labels; закрыть gate `overall_earnings_autoprep_ready` (34→40 LLM labels).

**Prod snapshot:** 35 extract, 34 labels, 30 pending (21 future + 9 past без parsed materials).

---

## P0 — сегодня (код + deploy)

| # | Задача | Файлы | Критерий готовности |
|---|--------|-------|---------------------|
| P0.1 | Date tolerance ±1d KB↔materials | `services/earnings_event_date_match.py`, calendar, extract, sync | ARM 2026-05-05 pending закрывается материалами 2026-05-06 |
| P0.2 | Past-only в pipeline autoprep | `earnings_calendar_new_events.py`, autoprep | Cron не тратит ingest/extract на future events |
| P0.3 | Ops backfill 9 past events | prod commands | labels ≥ 40 или +6 extract |

## P1 — сразу после P0

| # | Задача | Файлы | Критерий готовности |
|---|--------|-------|---------------------|
| P1.1 | Discover после failed ingest | `run_earnings_intelligence_autoprep.py` | autoprep log: `materials_discover` после ingest rc≠0 |
| P1.1b | Fix discover SQL (`ed.knowledge_base_id`) | `discover_earnings_material_sources.py` | discover не падает на prod |
| P1.2 | Extract: skip не в limit | `extract_earnings_material_facts.py` | `--all-events` не сжигает limit на already-extracted |
| P1.3 | Zip unpack (ASML) | `earnings_material_parser.py` | `application/zip` → parsed text |

## P2 — следом (не в этом спринте)

- Fool 429 backoff / alternate transcript source
- Analyzer pipeline dashboard (pending/parsed/extracted/labeled)
- `overall_grid_ready`: nightly train без dry_run

---

## Deploy

1. Commit + `git push origin main`
2. `ssh gcp-lse "cd ~/lse && ./scripts/deploy_from_github.sh"`
3. Backfill на prod (см. P0.3 в runbook ниже)

### P0.3 backfill (после deploy)

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

## Метрики успеха

- `llm_scenario_labels` ≥ 40
- `pending_extract` past-only ≤ 3 (остаток — даты без SEC/Fool)
- autoprep: `parsed/downloaded` > 0 на backfill прогоне

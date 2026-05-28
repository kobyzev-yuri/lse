# Earnings Intelligence — план на 2026-05-29

Зафиксировано после prod eval (`4534589`–`ce6c037`): grid advisory ready, shadow gate passed на 27 matured, UI guide на `/earnings/guide`.

**Deploy только через:** `git push origin main` → на VM `./scripts/deploy_from_github.sh`.

---

## Цель дня

Довести вкладку `/earnings` до **честного prod UX** (P0): пользователь видит полный Brief, regression привязан к дате события, shadow gate не вводит в заблуждение. Параллельно — один прогон materials/extract для свежих earnings (DELL и др.).

---

## Утро — проверка prod (15 мин)

```bash
ssh ai8049520@104.154.205.58 "docker exec lse-bot bash -lc '
  cat /app/logs/ml/ml_data_quality/last_earnings_intelligence_prod_eval.json
  cat /app/logs/ml/ml_data_quality/last_earnings_scenario_shadow.json | python3 -c \"import json,sys; d=json.load(sys.stdin); print(d.get(\\\"aggregate\\\"), d.get(\\\"trading_gate\\\"))\"
'"
```

- `/earnings` — все 6 вкладок открываются без 503
- `/earnings/guide` — 200
- META Brief / NVDA Fusion — непустые данные

---

## P0 — код (приоритет, ~4–6 ч)

| # | Задача | Файлы | Критерий готовности |
|---|--------|-------|---------------------|
| 1 | **Brief: regression по event_date** | `earnings_intelligence.html`, опционально `event_reaction_catboost_signal.py` / API | На META 2026-04-29 виден pred 5d или явное «нет features для даты» |
| 2 | **Brief: полный Event Brief в UI** | `earnings_intelligence.html` | evidence_quotes, guidance, capex_notes, status partial |
| 3 | **Shadow: переименовать gate** | `earnings_intelligence.html`, `EARNINGS_UI_GUIDE.md` | Badge «Shadow quality · advisory only», не «Trading ready» |
| 4 | **Peer graph: «40 из N»** | `earnings_intelligence.html` | Счётчик рёбер в UI |
| 5 | **Spillover: sources из graph ∪ events** | `earnings_intelligence.html` или API | NVDA/META всегда в select |

**Коммит после P0:** один PR/commit «Earnings UI P0: brief regression by date + full brief panel».

---

## P1 — данные (после P0 или параллельно ops, ~1–2 ч)

| # | Задача | Команда на VM |
|---|--------|---------------|
| 6 | Materials + extract universe | `ML_READINESS_TRAIN_MODE=full python3 scripts/run_earnings_intelligence_prod_eval.py --skip-ml-refresh` |
| 7 | Проверить DELL / свежие KB | SQL: `earnings_material`, `earnings_event_detail` для event_date = сегодня−1…+0 |
| 8 | Fool 429 | Зафиксировать в backlog: backoff в `ingest_earnings_materials.py`, фильтр junk URL (ARM) |

**Критерий:** `symbols_without_materials` в readiness ↓, новые LLM tone на свежих событиях.

---

## P1 — ML layers tab (30–60 мин)

| # | Задача | Файлы |
|---|--------|-------|
| 9 | Добавить слои Shadow + Fusion + readiness path | `earnings_intelligence_api.py` → `get_ml_layers_status`, UI tab ML |

---

## Не делать завтра (явный scope cut)

- Подключение fusion/shadow к GAME_5M или portfolio execution
- Retrain classifier с новыми классами (ждём ≥30 labels)
- Portfolio cards performance (отдельная задача)

---

## Вечер — acceptance

- [ ] P0 пункты 1–3 сделаны и на prod после deploy
- [ ] `/earnings/guide` обновлён если менялось поведение UI
- [ ] `last_earnings_scenario_shadow.json` — n_matured не упал
- [ ] Краткая запись в `EARNINGS_INTELLIGENCE_PLAN.md` (секция «2026-05-29») при существенных изменениях

---

## Ссылки

- [EARNINGS_UI_GUIDE.md](./EARNINGS_UI_GUIDE.md) — вкладки, примеры, полный audit
- [EARNINGS_INTELLIGENCE_PLAN.md](./EARNINGS_INTELLIGENCE_PLAN.md) — roadmap
- Prod JSON: `/app/logs/ml/ml_data_quality/last_earnings_intelligence_readiness.json`

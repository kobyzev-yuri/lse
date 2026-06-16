# Календарь earnings → продакшн: план простым языком

**Обновлено:** 2026-06-16  
**Цель:** система сама подхватывает прошедшие отчёты из календаря, готовит ML и шлёт понятные алерты. Оператор только смотрит Telegram и раз в неделю — чеклист.

---

## Словарь (два непонятных термина)

### Якорь (anchor) и anchor fallback

**Якорь** — это «с какой свечи считать до и после отчёта», чтобы не подглядывать в будущее.

Пример: отчёт во вторник до открытия (BMO). Фичи для ML берём с **понедельничного close**, реакцию рынка — с **вторника и дальше**. Если перепутать день — модель учится на мусоре.

**Почему бывает `anchor_unresolved`:** в календаре дата «вторник», а в котировках дырка (выходной, нет истории, дата отчёта не совпала с торговым днём, нет timing в materials). Система не знает, к какому бару привязаться, и пропускает строку.

**Anchor fallback** — осторожное правило «если точный timing неизвестен или дата на выходные — привяжи к ближайшему торговому дню по стандартной логике BMO/после закрытия». Это не выдумывает данные, а **не выбрасывает** строку из-за мелкой несостыковки календаря. Покрытие ERD растёт (сейчас ~84% строк с features).

### Runbook (рунбук)

**Runbook** — короткая инструкция для человека: что нормально, что нет, что делать при алерте, какие команды на VM. Не код, а «если пришло X в Telegram → открой Y → при необходимости Z». См. [EARNINGS_CALENDAR_RUNBOOK.md](./EARNINGS_CALENDAR_RUNBOOK.md).

---

## Где мы сейчас (prod)

| Что | Статус |
|-----|--------|
| Календарь → KB → materials autoprep | ✅ cron каждые 2ч, past-only |
| Nightly ERD + ML train | ✅ cron разнесён, DB pool fix |
| `overall_grid_ready` + peer | ✅ |
| `llm_scenario_labels` (42) | ✅ порог 40 |
| `shadow_n_matured` (44) | ⏳ нужно 50, ~несколько дней |
| `overall_earnings_autoprep_ready` | ❌ только shadow |
| Phase C (Telegram brief после отчёта) | 🔧 в этом спринте |

---

## План работ (порядок выполнения)

### Этап 1 — Данные надёжнее (код, сразу)

| # | Задача | Зачем |
|---|--------|-------|
| 1.1 | **Anchor fallback** — выходные / нет timing → BMO, AMC → последний торговый день | меньше `anchor_unresolved`, больше строк для ML |
| 1.2 | **Skip битых PDF** (`short_text:0`) — не крутить ingest по кругу | autoprep без ложных `rc=1` |
| 1.3 | Порог алерта **labeling gaps** для anchor — осмысленный default | Telegram, если дырок снова много |

### Этап 2 — Оператор видит картину (код + cron)

| # | Задача | Зачем |
|---|--------|-------|
| 2.1 | **Runbook** в `docs/EARNINGS_CALENDAR_RUNBOOK.md` | не лезть в логи вслепую |
| 2.2 | **Ежедневный digest autoprep** в Telegram (1 раз/сутки) | pending / ingest / extract / gates |
| 2.3 | **Phase C brief** в Telegram после нового extract | «CIEN отчитался → сценарий + peers» |

### Этап 3 — Ждём календарь (пассивно, без кода)

| # | Что происходит | Критерий |
|---|----------------|----------|
| 3.1 | Nightly outcomes + ML refresh | `shadow_n_matured` → 50 |
| 3.2 | Telegram «Earnings autoprep gate OPEN» | `overall_earnings_autoprep_ready=true` |
| 3.3 | Новые past events из календаря | autoprep + brief автоматически |

### Этап 4 — После зелёного gate (следующий спринт)

- Fusion в UI (регрессия в %)
- Open-path MVP (отдельный контур)
- Phase D влияние на GAME_5M — только после backtest

---

## Критерий «продакшн steady state»

1. Все `overall_*` gates зелёные в `last_earnings_intelligence_readiness.json`
2. Telegram: digest раз в день + brief на новый extract + gate flip один раз
3. `event_reaction_earnings_labeling.log` без `too many clients`
4. Оператор по runbook: **ничего руками**, кроме редких инцидентов (ProxyAPI balance, Fool 429)

---

## Deploy

```bash
git push origin main
ssh ai8049520@104.154.205.58 "cd ~/lse && ./scripts/deploy_from_github.sh --force && bash setup_cron_docker.sh"
```

См. также [EARNINGS_CALENDAR_URGENT_FIX_PLAN.md](./EARNINGS_CALENDAR_URGENT_FIX_PLAN.md) (история P0–P3).

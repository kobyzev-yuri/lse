# Runbook: календарь earnings (оператор)

Краткая шпаргалка: что значат алерты и что делать. Подробный план — [EARNINGS_CALENDAR_PRODUCTION_PLAN.md](./EARNINGS_CALENDAR_PRODUCTION_PLAN.md).

---

## Нормальное состояние

- **Autoprep cron** (каждые 2ч): в логе `earnings_autoprep.log` — `steps` с rc=0 или предупреждения ingest (не критично, если discover отработал).
- **Nightly 23:33–23:58 MSK**: ERD build → outcomes → labels → ML refresh. В `event_reaction_earnings_labeling.log` строка `Готово: candidates=...` без `Traceback`.
- **Readiness:** `overall_grid_ready=true`, `labeling_gaps` пустой.
- **До prod gate:** `shadow_n_matured` растёт (~1 в день на зрелое событие); ждём **50**.

Проверка gates (на VM):

```bash
docker exec lse-bot cat /app/logs/ml/ml_data_quality/last_earnings_intelligence_readiness.json \
  | python3 -c "import json,sys; g=json.load(sys.stdin)['gates']; print('autoprep', g.get('overall_earnings_autoprep_ready')); print('shadow', (g.get('earnings_autoprep') or {}).get('shadow_n_matured'))"
```

---

## Telegram-алерты

### «Earnings autoprep gate OPEN»

**Значит:** инфраструктура готова (`overall_earnings_autoprep_ready=true`).  
**Действие:** ничего срочного. Дальше ждём новые даты в календаре; brief пойдут автоматически.

### «ERD labeling gaps»

**Значит:** много строк без котировок (`no_quotes`) или без якоря (`anchor_unresolved`).  
**Действие:**

1. Посмотреть JSON: `/app/logs/ml/ml_data_quality/last_erd_labeling_gap_alert.json`
2. Если `no_quotes` — дождаться Sunday `seed_quotes` или вручную:
   ```bash
   docker exec lse-bot python scripts/seed_quotes_for_event_reaction_dataset.py \
     --dataset-version v0_expanded_baseline --include-earnings-universe --days 450
   ```
3. Если `anchor_unresolved` — проверить, есть ли materials/extract для этих тикеров (autoprep log).

### «Earnings brief: SYMBOL DATE»

**Значит:** новый extract, краткая сводка сценария и peers.  
**Действие:** информационно. Полный brief: `http://104.154.205.58:8080/earnings` или API `/api/earnings/brief`.

### «Autoprep digest» (ежедневно)

**Значит:** сводка pending materials, шаги autoprep, gates.  
**Действие:** если `pending_calendar_events` > 10 несколько дней подряд — см. ProxyAPI / Fool 429 ниже.

### Cron watchdog

**Значит:** ошибка в одном из логов cron.  
**Действие:** открыть `logs/cron_watchdog.log`, найти файл-источник.  
**Не паниковать:** `possibly delisted` от yfinance в premarket — шум, игнорируется.

---

## Частые проблемы

| Симптом | Причина | Что делать |
|---------|---------|------------|
| `too many clients` в ERD log | пик нагрузки на PG | уже fix pool; если повторится — не гонять backfill вручную параллельно с nightly |
| `materials_ingest rc=1` | битые URL / PDF | discover в autoprep; ARM/META — skip rules |
| `materials_extract rc=2` | ProxyAPI balance | пополнить; см. `last_earnings_llm_balance_alert.json` |
| `shadow_n` не растёт | мало новых 5d outcomes | ждать; nightly force-outcomes 23:35 |
| Brief `status: not_found` | нет KB row или extract | autoprep для symbol; календарь yfinance |

---

## Ручные команды (только инцидент)

```bash
# Полный autoprep по прошлым событиям (долго, не в обычном режиме)
docker exec lse-bot python scripts/run_earnings_intelligence_autoprep.py --all-events --ingest-limit 80

# Пересчёт readiness
docker exec lse-bot python3 -c "
from report_generator import get_engine
from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness
write_earnings_intelligence_readiness(get_engine())
"

# Smoke brief META
docker exec lse-bot python scripts/build_earnings_event_brief.py --symbol META --event-date 2026-04-29
```

---

## Еженедельный чек (5 минут, понедельник)

1. Gates green? (команда выше)
2. `shadow_n_matured` ≥ 50 или растёт?
3. `last_earnings_intelligence_autoprep.json` — `pending_calendar_events` ≤ 5?
4. `earnings_autoprep.log` — нет повторяющихся extract rc=2?

Если всё да — режим **«ждём календарь»**, руками не трогаем.

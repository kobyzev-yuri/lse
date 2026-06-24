# Option Money Map — план

## Цель

Один экран: **тикер → фраза «где деньги» → ползунок экспираций → график OI**. Без LLM, PCR, max pain в лице пользователя.

## Фазы

| Фаза | Статус | Содержание |
|------|--------|------------|
| **1** | ✅ | Подписи калькулятора (страйк ≠ вход) |
| **2** | ✅ wireframe | `GET /api/options/map/{ticker}`, страница `/options/map` |
| **3** | 📋 schema + stub | Таблица `options_chain_oi_snapshot`, `scripts/snapshot_options_chain_oi.py` |
| **4** | ✅ | Миграция 031 на prod, cron 1×/день после US close |
| **5** | ✅ | Ползунок «время»: история OI из БД + сдвиг плит vs предыдущий снимок |
| **6** | ⏳ | Заменить `/options` главным экраном или редирект для casual users |

## API

```
GET /api/options/map/MU?expiration_date=2026-06-26
GET /api/options/map/MU?expiration_date=2026-06-26&snapshot_date=2026-06-24
GET /api/options/map/MU/snapshots?expiration_date=2026-06-26
```

Ответ: `summary_one_liner_ru`, `support_plate`, `resistance_ceiling`, `chart_bars`, `available_expirations`, `available_snapshot_dates`, `is_live`, `plate_shift_ru`, `flow_label`, `pcr_volume`.

Источник: **только Polygon** (OI).

## Cron OI (фаза 3–4)

**Зачем:** Polygon не отдаёт OI в прошлом — для «как плиты смещались за недели» нужны ежедневные снимки.

**Таблица:** `db/knowledge_pg/sql/031_options_chain_oi_snapshot.sql`

**Скрипт:** `scripts/snapshot_options_chain_oi.py`

```bash
# dry-run
docker exec lse-bot python scripts/snapshot_options_chain_oi.py --ticker MU --dry-run

# после миграции
docker exec lse-bot python scripts/snapshot_options_chain_oi.py --ticker MU --ticker NVDA
```

**Оценка объёма:** MU ~850 строк/экспирация × 2 экспирации × 5 тикеров ≈ 8.5k rows/день → ~3M rows/год. Приемлемо с индексом `(ticker, expiration_date, snapshot_date)`.

**Cron (пример для `ct.txt`):** будни 23:30 UTC после закрытия US options.

```
30 23 * * 1-5 cd @LSE_HOME@ && docker exec @CONTAINER@ python scripts/snapshot_options_chain_oi.py >> logs/options_oi_snapshot.log 2>&1
```

**Фаза 5 UI:** ползунок «дата снимка» (0 = live Polygon, 1+ = архив из БД); строка `plate_shift_ru` — сдвиг топ-плит vs предыдущий снимок.

## Ответы стейкхолдеру

- **Страйк long** ≠ цена входа; вход = премия × 100 × контракты.
- **yfinance** не годится для OI-плит; карта — Polygon.
- **LLM** в карте не используется; one-liner — шаблон.

## Файлы

| Файл | Роль |
|------|------|
| `services/options_money_map.py` | Логика плит + one-liner |
| `templates/options_map.html` | Wireframe UI |
| `scripts/snapshot_options_chain_oi.py` | Cron snapshot |
| `db/knowledge_pg/sql/031_*.sql` | Схема истории |

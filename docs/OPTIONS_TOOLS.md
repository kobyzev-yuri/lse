# Опционы: `/options` и `/options/map` — постановка, реализация, эксплуатация

**Статус:** в prod (2026-06-24): dual-column Polygon/yfinance, Money Map, OI cron.  
**Веб:** `/options` (сентимент + калькулятор), `/options/map` (карта денег).  
**Зависимость:** `POLYGON_API_KEY` + подписка Polygon **Options Starter+** для OI/volume.

**См. также:** [OPTIONS_MONEY_MAP.md](OPTIONS_MONEY_MAP.md) — карта OI, cron, ползунок истории.

---

## 1. Постановка задачи

### Задача 1 — Option Chain и рыночный сентимент

Анализ шорт-опционов по стокам из universe LSE. Эталонная доска — **Investing.com Option Chain** (call/put, strike, volume, bid/ask, OI):

| Источник | URL (пример MU) | Роль |
|----------|-----------------|------|
| **Investing.com** | [micron-tech-options](https://www.investing.com/equities/micron-tech-options) | HTML-таблица «как ордербук» — эталон постановки |
| **Yahoo / yfinance** | [finance.yahoo.com/quote/MU/options](https://finance.yahoo.com/quote/MU/options) | `Ticker.options` + `option_chain(date)` — strike, volume, OI, bid/ask, IV **без API-ключа** (задержка/дыры в bid вне RTH) |
| **Polygon snapshot** | `GET /v3/snapshot/options/MU` | Целевой источник для LSE: REST, пагинация, Greeks |

Требовалось:

1. Проверить, можно ли получать ту же доску через **Polygon API** (предпочтительнее парсинга HTML Investing).
2. На основе **Volume** и **Open Interest** по call/put выдавать аналитику:
   - куда целится рынок по бумаге;
   - где больше открывают позиций (перекос put vs call);
   - какие страйки — ключевые **барьеры** для цены;
   - простой **индикатор сентимента** (bullish / bearish / neutral).

### Задача 2 — Опционный калькулятор

Интерактивный расчёт P/L для:

- **Pure Put** — покупка одного put;
- **Put Spread** — long put + short put с более низким страйком.

Ввод: тикер, spot, даты earnings/экспирации (справочно), контракты, страйки и премии.  
Вывод: стоимость входа, breakeven, max loss/profit, таблица сценариев падения spot (0%, −2%, … −20%).

---

## 2. Решение по источнику данных

| Критерий | Investing.com | yfinance | Polygon reference | Polygon **snapshot** |
|----------|---------------|----------|-------------------|----------------------|
| Доступ | HTML | Python, без ключа | REST + ключ | REST + ключ + **Options-план** |
| Volume / OI | в таблице | да (`volume`, `openInterest`) | **нет** | да (`day.volume`, `open_interest`) |
| Bid/Ask/Last | да | да (bid/ask часто 0 вне сессии) | **нет** | `last_quote`, `last_trade` |
| Greeks / IV | нет | `impliedVolatility` | нет | да (Options-план) |
| Экспирации | UI dropdown | `Ticker.options` (19 для MU) | `reference/contracts` | фильтр `expiration_date` |

**Вывод:** для **Задачи 1 (сентимент)** нужен именно **Polygon snapshot** — аналог Investing. Reference API даёт только справочник контрактов (страйк, тип, дата), без live volume/OI.

**yfinance** — запасной/сверочный источник той же таблицы (см. `--compare-yfinance` в скрипте проверки), в прод-сентименте не используется.

| API | Назначение |
|-----|------------|
| `GET /v3/reference/options/contracts?underlying_ticker={T}` | список дат экспирации, метаданные контрактов (**не доска**) |
| `GET /v3/snapshot/options/{T}?expiration_date=...` | **live snapshot = Option Chain**: volume, OI, bid/ask, Greeks, underlying price |

Документация Polygon: [Options chain snapshot](https://polygon.io/docs/options/get_v3_snapshot_options__underlyingasset).

**Ограничение snapshot API:** исторический OI за прошлые недели **не запрашивается** — только текущий снимок. История накапливается cron → `options_chain_oi_snapshot` (см. [OPTIONS_MONEY_MAP.md](OPTIONS_MONEY_MAP.md)).

### Проверка после оплаты подписки Options

```bash
# на GCP VM
docker exec lse-bot python scripts/verify_polygon_options_chain.py --ticker MU --compare-yfinance
```

Ожидаемый результат:
- `polygon_snapshot.status` = `ok`
- `contract_count` > 0 (для MU на одну экспирацию — десятки/сотни call+put)
- `contracts_with_oi` / `contracts_with_volume` > 0
- `VERDICT: OK — доска как Investing`

Сверка с yfinance на ту же дату: порядок величины calls/puts (у MU ~300+ строк на экспирацию в yfinance).

Если всё ещё **403 / NOT_AUTHORIZED** при оплаченном счёте — почти всегда оплачен **Stocks**, а не **Options** (отдельный продукт ~$29/мес Starter). Проверка на VM:

```bash
docker exec lse-bot python scripts/verify_polygon_options_chain.py --ticker MU --compare-yfinance
```

В [dashboard](https://polygon.io/dashboard) → Billing / Subscriptions должны быть **два** продукта (или bundle): Stocks **и** Options. Один API key — entitlements с обоих планов.

---

## 3. Архитектура

```mermaid
flowchart TB
    UI["/options templates/options.html"]
    API["web_app.py routes"]
    POLY["services/polygon_options.py"]
    SENT["services/options_chain_sentiment.py"]
    CALC["services/options_calculator.py"]
    MAP["services/options_money_map.py"]
    PG["api.polygon.io"]
    DB["options_chain_oi_snapshot"]

    UI --> API
    API --> POLY
    API --> SENT
    API --> CALC
    API --> MAP
    POLY --> PG
    SENT --> POLY
    MAP --> POLY
    MAP --> DB
```

### Файлы репозитория

| Файл | Роль |
|------|------|
| `services/polygon_options.py` | клиент Polygon: expirations, chain snapshot, нормализация контрактов |
| `services/options_chain_sentiment.py` | PCR, max pain, ключевые страйки, sentiment score |
| `services/options_calculator_prefill.py` | prefill премий с Polygon / yfinance |
| `services/options_money_map.py` | Option Money Map (плиты OI) |
| `templates/options.html` | UI: две вкладки, dual-column compare |
| `templates/options_map.html` | UI карты денег |
| `web_app.py` | маршруты страниц и JSON API |
| `templates/partials/site_nav_links.html` | «Опционы», «Опционы · карта» |
| `scripts/snapshot_options_chain_oi.py` | ежедневный снимок OI в PostgreSQL |
| `tests/test_options_tools.py` | unit-тесты |
| `config.env.example` | `POLYGON_API_KEY` (секрет только в `config.env` на VM, не в git) |

---

## 4. Вкладка «Опционы» в веб-UI

**URL:** `/options` и `/options/map` (на проде порт `8080` внутри VM).

### 4.1. Вкладка «Сентимент chain»

Две колонки: **Polygon snapshot** | **yfinance option_chain**.

1. Поле **Тикер** (по умолчанию `MU`).
2. **Экспирация** — dropdown; кнопки «Polygon» / «yfinance» / **«Оба»** для дат.
3. «Анализ» — по источнику или **«Оба»** параллельно.

**На экране:**

- бейдж `BEARISH` / `BULLISH` / `NEUTRAL` + score −1…+1;
- PCR vol, PCR OI, spot, max pain;
- таблица топ-страйков: **Call OI / Put OI** (Polygon) или **Call vol / Put vol** (yfinance без OI);
- после «Оба» — баннер сравнения (разный spot, разный состав score);
- **LLM интерпретация** по кнопке в каждой колонке.

### 4.2. Вкладка «Калькулятор»

Две колонки prefill + сводная таблица P/L. Подписи: **страйк ≠ вход**; вход = премия × 100 × контракты.

Кнопки: загрузка дат, **Polygon** / **yfinance** / **Оба** для prefill, календарь earnings из knowledge_base.

### 4.3. Option Money Map (`/options/map`)

Отдельная страница: one-liner, график OI, put-плита / call-потолок, ползунки **экспирации** и **даты снимка** (0 = live). Подробности и пример MU — [OPTIONS_MONEY_MAP.md](OPTIONS_MONEY_MAP.md).

---

## 5. Реальный пример торговли: MU put spread перед экспирацией

**Контекст:** MU ~$1 093, экспирация **2026-06-26**, доска Polygon (2026-06-24).

### Prefill с Polygon

`GET /api/options/calculator/polygon-prefill/MU?expiration_date=2026-06-26&strategy=put_spread`

| Поле | Значение |
|------|----------|
| Spot | $1 093.59 |
| Long put K | $1 095, премия **$91.38** |
| Short put K | $1 040, премия **$61.60** |

### Расчёт (`POST /api/options/calculator`, 1 контракт)

| | |
|--|--|
| Вход (net debit) | **$2 978** |
| Breakeven | **$1 065.22** (−2.6% от spot) |
| Max loss | **$2 978** |
| Max profit | **$2 522** (ширина $55 − debit) |

**Сценарии intrinsic на экспирацию:**

| Падение spot | Цена | P/L | Смысл |
|--------------|------|-----|--------|
| 0% | $1 093.59 | **−$2 837** | Далеко OTM — почти полная потеря премии |
| −2% | $1 071.72 | **−$650** | Ещё ниже breakeven |
| −3% | $1 060.78 | **+$444** | Начало зоны прибыли |
| −5% | … | растёт | Спред ловит падение без полной премии naked put |

**Связь с картой OI:** put-плита на **$1 000** (OI 8 480) — зона, куда рынок концентрировал защиту; max pain сентимента **$1 050** — эвристический «магнит» на экспирацию. Это **не** сигнал входа; калькулятор показывает только P/L при заданных премиях.

### Сентимент на той же дате (Polygon vs yfinance)

| | Polygon | yfinance |
|--|---------|----------|
| Spot | $1 093 | $1 052 |
| Score | BULLISH **0.43** | NEUTRAL **0.14** |
| PCR vol | 0.65 | 0.89 |
| Max pain | $1 050 | — (нет OI) |

Put spread — ставка на **падение**; при BULLISH call-flow на доске это контртренд — осознанный риск, а не противоречие в данных.

---

## 6. REST API

### `GET /api/options/expirations/{ticker}?source=polygon|yfinance`

```json
{
  "ticker": "MU",
  "expirations": ["2026-06-26", "2026-07-03", "..."]
}
```

Если ключа нет: `expirations: []`, `error: "POLYGON_API_KEY not configured"`.

### `GET /api/options/sentiment/{ticker}?source=polygon|yfinance&expiration_date=2026-06-26`

Пример (MU, Polygon, prod 2026-06-24):

```json
{
  "status": "ok",
  "ticker": "MU",
  "source": "polygon",
  "expiration_date": "2026-06-26",
  "spot": 1093.0,
  "sentiment_label": "BULLISH",
  "sentiment_score": 0.427,
  "totals": {
    "pcr_volume": 0.647,
    "pcr_open_interest": 0.779
  },
  "max_pain_strike": 1050.0,
  "key_strikes_oi": [
    {"strike": 1050.0, "total_oi": 18858, "call_oi": 16500, "put_oi": 2358}
  ]
}
```

### `POST /api/options/calculator`

Тело (Pure Put):

```json
{
  "strategy": "pure_put",
  "ticker": "MU",
  "spot": 189.0,
  "contracts": 2,
  "long_strike": 190.0,
  "long_premium": 8.5,
  "earnings_date": "2026-06-25",
  "expiration_date": "2026-06-26"
}
```

Тело (Put Spread) — дополнительно `short_strike`, `short_premium`; требование `long_strike > short_strike`.

Ответ: `entry_cost_usd`, `breakeven`, `max_loss_usd`, `max_profit_usd`, массив `scenarios`.

---

## 7. Логика сентимента

Реализация: `analyze_options_chain()` в `services/options_chain_sentiment.py`.

| Метрика | Смысл |
|---------|--------|
| **PCR volume** | put_volume / call_volume |
| **PCR OI** | put_oi / call_oi |
| **NTM PCR** | то же в полосе ±8% от spot |
| **Max pain** | страйк S, минимизирующий суммарную выплату call/put держателям |
| **Ключевые страйки** | топ по total OI и по total volume |

**sentiment_score** — среднее по нормированным PCR (put-heavy → отрицательный score → `BEARISH`).

Окно страйков в отчёте: ±15% от spot (`strike_window_pct` в `build_chain_sentiment_report`).

---

## 8. Логика калькулятора

`services/options_calculator.py`

| Стратегия | Вход | Breakeven | Max loss | Max profit |
|-----------|------|-----------|----------|------------|
| Pure Put | premium × 100 × N | K − premium | = вход | не ограничен (в UI «∞») |
| Put Spread | (long_prem − short_prem) × 100 × N | K_long − net_debit | = вход | (width − net_debit) × 100 × N |

Сценарии: `SCENARIO_DROP_PCTS = (0, -2, -3, -5, -7, -8, -10, -12, -15, -20)`.

---

## 9. Конфигурация и прод

### config.env (на VM, не в git)

```env
POLYGON_API_KEY=<ключ из https://polygon.io/dashboard/api-keys>
```

Регистрация: [polygon.io/dashboard/signup](https://polygon.io/dashboard/signup).  
Ключи: [polygon.io/dashboard/api-keys](https://polygon.io/dashboard/api-keys).

После изменения `config.env`:

```bash
docker compose restart lse   # на GCP VM в каталоге lse
```

### Деплой кода

```bash
git push origin main
ssh <vm> "cd ~/lse && ./scripts/deploy_from_github.sh"
```

---

## 10. Тесты

```bash
pytest tests/test_options_tools.py -v
```

Покрытие: breakeven/max loss Pure Put, max profit Put Spread, bearish sentiment при put-heavy OI, валидация страйков спреда.

---

## 11. Дальнейшие улучшения

- Фаза 6: `/options/map` как главный вход для casual users.
- Накопление истории OI (cron, без fake backfill) — см. [OPTIONS_MONEY_MAP.md](OPTIONS_MONEY_MAP.md).
- Учёт IV crush / временной стоимости в калькуляторе.
- Интеграция сентимента в карточки GAME_5M / earnings brief.

---

## 12. Связанные документы

- [OPTIONS_MONEY_MAP.md](OPTIONS_MONEY_MAP.md) — карта денег, cron, пример MU

- [README.md](README.md) — навигация по документации
- [PORTFOLIO_GAME.md](PORTFOLIO_GAME.md) — портфельная игра (отдельный контур)
- [GAME_5M_WEB_CARDS.md](GAME_5M_WEB_CARDS.md) — веб-карточки 5m

# Polygon Options vs yfinance — оценка перед отказом от подписки

**Контекст:** подписка Polygon Options оплачена ещё ~1 месяц. Цель — понять, что **реально даёт только Polygon**, и можно ли через месяц перейти на yfinance для опционов.

**Проверка на prod (MU, exp 2026-06-26, 2026-06-25 after-hours):**

| Метрика | Polygon snapshot | yfinance option_chain |
|---------|------------------|------------------------|
| Контрактов на exp | **862** | 676 |
| С OI > 0 | 733 | 665 |
| С volume > 0 | 794 | 668 |
| С bid > 0 | 0* | **531** |
| С ask > 0 | 0* | **676** |
| **Delta (Greeks)** | **585** | **0** |
| IV | 585 | 676 |
| Spot | $1199 (snapshot/stocks) | $1214 (Yahoo quote) |
| Список exp (MU) | **2** (reference API†) | **19** |

\* Вне RTH у Polygon `last_quote` часто пустой → bid/ask=0; днём картина другая.  
† Reference `/v3/reference/options/contracts` отдаёт exp по первым N контрактам; для дальних LEAPS yfinance полнее.

---

## 1. Что совпадает (yfinance «на том же уровне»)

| Возможность | Комментарий |
|-------------|-------------|
| **Open Interest** | Один clearing (OCC) — топ страйков **идентичен** |
| **Volume / PCR vol** | Близко; yfinance иногда чуть ниже по строкам (фильтр zero OI+vol) |
| **Max pain, PCR OI** | При наличии OI формулы те же |
| **IV** | yfinance: `impliedVolatility` по всем строкам; Polygon: `implied_volatility` (~585/862) |
| **Калькулятор P/L** | Полностью на премиях — yfinance **достаточен** (mid bid/ask) |
| **Сентимент label/score** | Часто совпадает (BULLISH/NEUTRAL); расходятся spot и volume |

**Вывод:** для **ручного** `/options/tools` (сентимент + калькулятор) yfinance **уже конкурентен**.

---

## 2. Что есть только у Polygon (или сильно лучше)

| # | Возможность | Где в LSE | Критичность для отказа |
|---|-------------|-----------|-------------------------|
| 1 | **Greeks (delta и др.)** в snapshot | `polygon_options._normalize_contract_row` → `options_card_context` | Низкая сегодня (не в UI сентимента/калькулятора); может пригодиться для gate/IV-анализа |
| 2 | **REST snapshot + пагинация** | `fetch_options_chain_snapshot`, cron OI | Средняя: 862 vs 676 строк; стабильнее для **автоматики** |
| 3 | **Stocks snapshot spot** fallback | `fetch_polygon_stock_spot` | Низкая: yfinance spot обычно лучше для equity |
| 4 | **Vendor SLA / без скрейпа Yahoo** | cron `snapshot_options_chain_oi.py`, money map live | **Высокая для prod-cron** — yfinance неофициальный, может throttle/ломаться |
| 5 | **Единый API key в коде** | earnings brief `options_polygon`, options gate shadow | Средняя: сейчас hard dependency на `polygon_options_available()` |
| 6 | **OI history cron** | `options_chain_oi_snapshot` | **yfinance** (`snapshot_options_chain_oi.py`, cron 23:30 UTC) |

### Где код **жёстко** завязан на Polygon

- `services/options_money_map.py` — live без ключа → error
- `scripts/snapshot_options_chain_oi.py` — **yfinance** (история OI в БД)
- `services/options_card_context.py` / `earnings_event_brief.attach_options_polygon_to_brief`
- `services/options_calculator_prefill.suggest_expiration` — fallback `polygon_reference`
- Default source в API: `source=polygon`

---

## 3. Что лучше у yfinance

| # | Возможность | Комментарий |
|---|-------------|-------------|
| 1 | **Больше дат экспирации** | MU: 19 vs 2 в Polygon reference |
| 2 | **Bid/ask вне Polygon quote** | After-hours: yfinance 531/676 bid/ask vs 0 у Polygon last_quote |
| 3 | **Бесплатно, без ключа** | Новости (`get_news`), earnings, option_chain |
| 4 | **Премии mid(bid/ask)** | Калькулятор: часто реалистичнее для входа, чем Polygon `last` |

---

## 4. Рекомендация на оставшийся месяц

### Использовать Polygon пока оплачен

1. **Ежедневно:** OI cron → копить `options_chain_oi_snapshot` (история только в нашей БД).
2. **Раз в неделю:** `verify_polygon_options_chain.py --ticker MU --compare-yfinance` + 1–2 тикера GAME_5M — логировать в `logs/polygon_eval/`.
3. **Сравнить в RTH:** bid/ask Polygon vs yfinance (после 16:30 ET) — сейчас after-hours искажает в пользу yfinance.

### Критерии «можно отказаться через месяц»

| Критерий | Порог |
|----------|--------|
| OI топ-5 страйков | Совпадают ≥4/5 дней на MU+AMD |
| Volume PCR | Расхождение ≤0.1 в окне ±15% |
| Cron yfinance OI | Реализован и 5 успешных nightly без ошибок |
| Money map | Работает на yfinance fallback (код) |
| Greeks | Не нужны в prod (или IV из yfinance достаточен) |

### Если отказываемся — минимальный объём работ

1. `snapshot_options_chain_oi.py` → **yfinance** (сделано 2026-06-25).
2. `build_money_map_report` → yfinance fallback при отсутствии Polygon.
3. `options_card_context` / earnings brief → yfinance chain или `gate_hint=unavailable`.
4. Expirations UI → default `yfinance` (богаче список exp).

**Оценка:** 0.5–1.5 дня разработки + неделя shadow на prod.

---

## 5. Новости (отдельно от Polygon)

Polygon **не заменяет** NewsAPI/Investing news. Бесплатный стек новостей:

- `fetch_news_cron --mode core-fast` — RSS + yfinance earnings
- `fetch_news_cron --mode tickers` — `yfinance.get_news()` по тикерам
- Investing calendar JSON — без ключа

См. `crontab/lse-docker.crontab` (обновлено 2026-06-25).

---

## 6. Краткий вердикт

| Контур | yfinance enough? | Polygon still worth $? |
|--------|------------------|------------------------|
| Сентимент + калькулятор (UI) | **Да** | Нет, если dual-column = yfinance-only |
| Money Map live | **Почти** (OI тот же) | Слабо |
| OI cron / история | **Да после patch** | Нет после patch |
| Earnings brief options block | **Нет без patch** | Да, пока не перепишем |
| Greeks / delta analytics | **Нет** | **Да**, если понадобится |

**Практично:** месяц — **shadow-период**: cron Polygon OI копим, параллельно можно добавить yfinance OI cron в shadow (dry-run) и сравнивать. Решение об отмене — по таблице в §4.

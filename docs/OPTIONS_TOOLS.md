# Опционы: Polygon chain + калькулятор Put

## Задача 1 — Option Chain / сентимент

**Вывод:** доска опционов есть в Polygon API — проще, чем парсить Investing.com.

| Источник | Endpoint | Поля |
|----------|----------|------|
| **Polygon snapshot** | `GET /v3/snapshot/options/{underlying}` | volume, open_interest, bid/ask, Greeks, IV |
| Reference (даты) | `GET /v3/reference/options/contracts` | expiration_date, strike, contract_type |

Код: `services/polygon_options.py`, аналитика: `services/options_chain_sentiment.py`.

### Метрики на выходе

- **PCR volume / PCR OI** — put/call ratio
- **NTM PCR** — перекос вблизи денег (±8% от spot)
- **Ключевые страйки** — топ по OI и по volume (барьеры)
- **Max pain** — страйк с минимальной выплатой держателям
- **sentiment_score** ∈ [-1, 1], label `BEARISH` / `BULLISH` / `NEUTRAL`

### UI / API

- Страница: `/options` → вкладка «Сентимент chain»
- `GET /api/options/expirations/{ticker}`
- `GET /api/options/sentiment/{ticker}?expiration_date=YYYY-MM-DD`

### Конфиг

```env
POLYGON_API_KEY=...
```

Подписка **Options** на polygon.io (от ~$29/mo, OI/volume на delayed tiers).

---

## Задача 2 — Калькулятор Put / Put Spread

Код: `services/options_calculator.py`

- **Pure Put** — long put, breakeven = K − premium
- **Put Spread** — long K₁ + short K₂ (K₁ > K₂), max profit = (width − net debit) × 100 × contracts

Сценарии падения: 0%, −2%, −3%, −5%, −7%, −8%, −10%, −12%, −15%, −20% (intrinsic на экспирацию).

- `POST /api/options/calculator` — JSON body
- UI: `/options` → вкладка «Калькулятор»

---

## Тесты

`pytest tests/test_options_tools.py`

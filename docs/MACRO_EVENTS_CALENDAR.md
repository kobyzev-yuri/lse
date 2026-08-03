# Macro events calendar: Investing vs FOMC/FRED/Yahoo

Reference UI mock: [`docs/notebook_events_calendar_mock.html`](notebook_events_calendar_mock.html) (from Telegram `calendar.html`).

Python adapters: [`services/macro_events_calendar.py`](../services/macro_events_calendar.py),
KB writer for official sources: [`services/official_macro_calendar_kb.py`](../services/official_macro_calendar_kb.py).

## What LSE needs

Same information density as the mock: dated rows with kind dots — **economic / FOMC / earnings** — for notebook Verdict + Environment, plus KB rows for ML features and news gates.

## Shared store: `knowledge_base`

| Source | Writer | `source` column | Role |
|--------|--------|-----------------|------|
| **FRED** | `official_macro_calendar_kb` (cron `--mode investing`) | `FRED Economic Calendar (USA)` | Primary US economic schedule in KB |
| **FOMC** | same | `FOMC Calendar (USA)` | Primary Fed decision dates in KB |
| **Investing.com** | `investing_calendar_parser` | `Investing.com Economic Calendar ({region})` | Optional enricher (multi-region, consensus); often 403 from GCP without proxy |
| **Yahoo** | live in UI / separate earnings cron | — | Earnings dots (not required for macro KB) |

**Consumers (all use the same KB source filter):**

- Notebook `/notebook` boot + `GET /api/notebook/calendar` — prefer KB; live FRED/FOMC only if KB empty for that kind
- `ingest_macro_calendar_daily_features.py` → `macro_calendar_daily_features`
- `kb_news_report.fetch_kb_macro_calendar_upcoming`

## Comparison

| Source | What | Pros | Cons | Role in LSE |
|--------|------|------|------|-------------|
| **FRED** | US release dates (CPI, NFP, PCE, …) | Official API, stable from GCP | Needs `FRED_API_KEY`; no consensus | **Primary** KB + notebook economic |
| **Fed.gov FOMC** | Meeting / decision dates (+ SEP) | Stable HTML, no API key | Only FOMC | **Primary** KB + Verdict ФРС |
| **Investing.com** | Full calendar + actual/forecast | Rich fields, multi-region | Cloudflare 403 from datacenter IPs | Optional KB enricher |
| **Yahoo / yfinance** | Next earnings per ticker | Matches notebook symbols | Soft dates / rate limits | Earnings rows (UI) |

## Config

- `FRED_API_KEY` (or `FRED_KEY`) — in `config.secrets.env`; without it FRED rows are skipped (FOMC still works).
- Cron: `fetch_news_cron.py --mode investing` runs official FRED+FOMC **before** Investing.
- Cache ~1h in-process for live FOMC/FRED fallback only.

## API / UI

- Boot payload: `calendar` + `env_live.fomc`
- `GET /api/notebook/calendar?days=21`
- Notebook: Events panel on **Вердикт** and **Окружение**

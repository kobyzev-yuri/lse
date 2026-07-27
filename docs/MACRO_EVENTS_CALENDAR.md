# Macro events calendar: Investing vs FOMC/FRED/Yahoo

Reference UI mock: [`docs/notebook_events_calendar_mock.html`](notebook_events_calendar_mock.html) (from Telegram `calendar.html`).

Python port of Go adapters (`wintermonth2298/marketdata`): [`services/macro_events_calendar.py`](../services/macro_events_calendar.py).

## What LSE needs

Same information density as the mock: dated rows with kind dots — **economic / FOMC / earnings** — for notebook Verdict + Environment.

## Comparison

| Source | What | Pros | Cons | Role in LSE |
|--------|------|------|------|-------------|
| **Investing.com** | Full economic calendar (importance, actual/forecast, multi-region) | Already in cron → `knowledge_base`; rich fields | 429/403, brittle HTML | **Primary** ingest |
| **Fed.gov FOMC** | Official meeting / decision dates (+ SEP) | Stable HTML, no API key | Only FOMC | **Live ФРС** on Verdict |
| **FRED** | US release dates (CPI, NFP, PCE, …) | Official API | Needs `FRED_API_KEY`; no consensus | Fallback macro |
| **Yahoo / yfinance** | Next earnings per ticker | Matches notebook symbols | Soft dates / rate limits | Earnings rows |

**Verdict:** keep Investing for KB depth; use FOMC+FRED+Yahoo for live UI and when Investing is empty.

## Config

- `FRED_API_KEY` — optional; without it economic rows = FOMC + earnings only.
- Cache ~1h in-process for FOMC/FRED.

## API / UI

- Boot payload: `calendar` + `env_live.fomc`
- `GET /api/notebook/calendar?days=21`
- Notebook: Events panel on **Вердикт** and **Окружение** (calendar.html style)

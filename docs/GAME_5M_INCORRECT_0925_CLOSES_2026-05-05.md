## Summary

On `2026-05-05` several `GAME_5M` positions were closed on the first cron run after the NYSE open.
In the DB / reports this appeared as **Close time = 09:25 ET (16:25 MSK)** for multiple tickers (MU, ASML, SNDK, NBIS).
This is confusing for management reporting (“sold before the session open”) and, more importantly, can accidentally use
the **09:25–09:30 premarket** 5-minute window for take/stale decisions.

## What actually happened (root cause)

- `send_sndk_signal_cron.py` is scheduled **every minute** on the production VM (`crontab -l` shows `*/1 * * * 1-5 ...`).
- `get_decision_5m()` builds the “exit bar” window using a clock grid:
  - `bar_end = now_et.floor("5min")`
  - `bar_start = bar_end - 5 minutes`
- On the first run right after `09:30 ET`, `bar_end` becomes `09:30`, so the chosen window is **`[09:25; 09:30)`**.
- That window is the 5-minute bucket that ends at the open. Even if Yahoo/yfinance treats it as part of
  the first “regular session” aggregation (bar timestamp is the bar open), it still creates a misleading
  close time label (`09:25`) in reports, and can mix boundary behavior around the open.
  It is *not* the first completed regular-session bar (`[09:30; 09:35)`), because the first RTH 5-minute bar closes only at `09:35`.

Result:

- The cron made exit decisions during `NEAR_OPEN` while using the premarket bar window.
- In `trade_history.ts` we record the **bar open** (`exit_5m_bar_open_et`), so the stored close time becomes `09:25 ET`.

## Evidence (production DB + cron logs)

- In `trade_history.context_json` for the problematic SELL rows:
  - `exit_bar_start_et = 2026-05-05T09:25:00-04:00`
  - `exit_bar_end_et   = 2026-05-05T09:30:00-04:00`
- In `logs/cron_sndk_signal.log` around `16:30 MSK` (first run after open):
  - exits show `exit_bar_et=[2026-05-05T09:25:00-04:00..2026-05-05T09:30:00-04:00)`

## Replay evidence (RTH-only, what would happen without the 09:25 bucket)

We ran a bar-by-bar replay using `scripts/replay_incident_2026_05_05_open_closes.py`:

- Source of truth for positions: `trade_history` (last BUY before the incident SELL + the incident SELL itself).
- Price source for replay: **5m OHLC**, restricted to **RTH only** for `2026-05-05` (`[09:30..16:00] ET`).
- Exit decisions: `services.game_5m.should_close_position(..., simulation_time=bar_end_et)` called per bar close.

Result (RTH-only replay) vs recorded incident (recorded window is always `[09:25..09:30)` in `context_json`):

- **MU**
  - recorded: `16:25 MSK`, `TAKE_PROFIT_SUSPEND`, window `[09:25..09:30)`
  - replay: **`TAKE_PROFIT`** on **09:30–09:35**, fill `617.5199`
- **NBIS**
  - recorded: `16:25 MSK`, `TIME_EXIT_EARLY stale_reversal`, window `[09:25..09:30)`
  - replay: **`TIME_EXIT_EARLY stale_reversal`** on **09:30–09:35**, fill `171.5188`
- **ASML**
  - recorded: `16:25 MSK`, `TAKE_PROFIT_SUSPEND`, window `[09:25..09:30)`
  - replay: **`TAKE_PROFIT`** on **11:45–11:50**, fill `1447.0000` (i.e. not at the open)
- **SNDK**
  - recorded: `16:25 MSK`, `TAKE_PROFIT`, window `[09:25..09:30)`
  - replay: **`TAKE_PROFIT`** on **09:30–09:35**, fill `1316.0000`

This demonstrates the intent of the fix: if we avoid making decisions on the ambiguous `[09:25..09:30)` bucket, the earliest valid bar-based decision time becomes the first completed RTH bar close at `09:35 ET` (i.e. bar `09:30–09:35`).

### Export for reporting (JSON/CSV + PnL deltas)

The script was extended to compute:

- `log_return`: \( \ln(\text{exit}/\text{entry}) \)
- `gross_pnl_usd`: `quantity * (exit - entry)`
- deltas vs DB (`delta_log_return`, `delta_gross_pnl_usd`) between recorded DB exit fill and replay fill

Example run (inside `lse-bot` container or any env with DB access and deps):

```bash
python3 scripts/replay_incident_2026_05_05_open_closes.py \
  --tickers MU,NBIS,ASML,SNDK \
  --json /tmp/game5m_incident_replay_2026-05-05.json \
  --csv /tmp/game5m_incident_replay_2026-05-05.csv
```

## Why it is considered incorrect

- For reporting: it looks like trades were closed before the market open.
- For the algorithm: using `[09:25;09:30)` can pull **premarket** highs/lows into TAKE / stale-reversal triggers.

## Fix implemented

We added an **exit guard in `services/game_5m.should_close_position()`**:

- If `session_phase == "NEAR_OPEN"` and we are within the first `N` minutes since `09:30 ET`
  (default `N=5`, configurable via `GAME_5M_EXIT_GUARD_FIRST_MINUTES`),
  then we **skip all exit checks** (TAKE/STOP/TIME_EXIT/TIME_EXIT_EARLY) for that run.

This preserves the intended “bar-based” semantics: the first regular-session bar closes at `09:35`,
so any exit that relies on 5m OHLC should not fire at `09:30:xx`.

## Operational note (cron frequency)

Even with the guard, running the cron every minute is usually unnecessary because 5m OHLC changes every 5 minutes.
If desired, reduce crontab to `*/5` (the repository helper `setup_cron_docker.sh` already uses `*/5`).
Also update the repo template `crontab/lse-docker.crontab` to avoid reintroducing `*/1` later.

## Follow-ups (optional)

- Add a “bar boundary / id” cache so minute-level cron runs do not re-evaluate exits within the same 5m bar.
- Consider separating “reporting timestamp” (bar end) from DB `trade_history.ts` (bar open) explicitly in UI exports.


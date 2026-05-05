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
- That window is mostly **premarket**. It is *not* the first completed regular-session bar (`[09:30; 09:35)`),
  because the first RTH 5-minute bar closes only at `09:35`.

Result:

- The cron made exit decisions during `NEAR_OPEN` while using the premarket bar window.
- In `trade_history.ts` we record the **bar open** (`exit_5m_bar_open_et`), so the stored close time becomes `09:25 ET`.

## Evidence (production DB + cron logs)

- In `trade_history.context_json` for the problematic SELL rows:
  - `exit_bar_start_et = 2026-05-05T09:25:00-04:00`
  - `exit_bar_end_et   = 2026-05-05T09:30:00-04:00`
- In `logs/cron_sndk_signal.log` around `16:30 MSK` (first run after open):
  - exits show `exit_bar_et=[2026-05-05T09:25:00-04:00..2026-05-05T09:30:00-04:00)`

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

## Follow-ups (optional)

- Add a “bar boundary / id” cache so minute-level cron runs do not re-evaluate exits within the same 5m bar.
- Consider separating “reporting timestamp” (bar end) from DB `trade_history.ts` (bar open) explicitly in UI exports.


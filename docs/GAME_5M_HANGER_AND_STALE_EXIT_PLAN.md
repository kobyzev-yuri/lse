# GAME_5M: rework hanger and stale exits

## Problem

Current GAME_5M has two separate issues that are mixed together in reports:

1. **Hanger**: a position waits for a take-profit for too long, tying up capital.
2. **Stale reversal**: a position no longer has a positive setup, receives weak or opposite signals, and keeps falling instead of recovering.

The current `hanger` mechanism is not a predictive model. It is a late classifier:

- replay current rules over a calendar window;
- if the position did not close by replay;
- and the market is not above entry at the end of the window;
- then classify it as `hanger`.

The current remediation only lowers the take-profit cap for classified hangers. That can help positions that are close to breakeven or mildly positive, but it does not solve deep negative reversals like NBIS or ASML.

## Current Weak Spots

- `GAME_5M_EXIT_ONLY_TAKE=true` disables `TIME_EXIT`, `TIME_EXIT_EARLY`, `STOP_LOSS`, and SELL-based exits after the take-profit check. Per-ticker max hold minutes therefore do not protect capital while this flag is enabled.
- `hanger` is detected too late: default live window is 6 calendar days.
- `hanger` only changes take-profit cap. It does not close bad positions.
- Current SELL decision is explicitly not used as a close trigger for an already open long.
- There is no first-30/60-minute stuck-risk score to predict a future hanger early.

## Target Behavior

Split exit logic into three independent layers:

1. **Normal profit capture**
   - Use existing dynamic take-profit.
   - Keep soft take near intraday high.

2. **Hanger rescue**
   - Keep current `TAKE_PROFIT_SUSPEND` idea.
   - Use it only for positions that are not deeply broken and can plausibly recover to a smaller positive take.

3. **Stale/reversal exit**
   - Close positions that exceed their expected holding time and no longer have supportive signals.
   - This is not a stop-loss. It is a time-and-signal invalidation rule.
   - Exit as `TIME_EXIT_EARLY` initially to reuse existing reporting, then optionally introduce `STALE_REVERSAL_EXIT`.

## Phase 1: Immediate Risk Control

Implement a stale/reversal rule in `services/game_5m.py::should_close_position`.

Suggested condition:

```text
enabled = GAME_5M_STALE_REVERSAL_EXIT_ENABLED
age >= GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES
pnl_current_pct <= GAME_5M_STALE_REVERSAL_MAX_PNL_PCT
current_decision in HOLD/SELL
momentum_2h_pct <= GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW
```

Initial defaults for production tuning:

```env
GAME_5M_STALE_REVERSAL_EXIT_ENABLED=true
GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES=390
GAME_5M_STALE_REVERSAL_MAX_PNL_PCT=-1.5
GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW=0.0
```

For high-risk tickers, use stricter per-ticker max hold:

```env
GAME_5M_MAX_POSITION_MINUTES_ASML=390
GAME_5M_MAX_POSITION_MINUTES_NBIS=780
```

Implementation note: the stale/reversal rule should be checked before the broad `GAME_5M_EXIT_ONLY_TAKE` guard, so enabling the rule can still protect stale risk even if legacy take-only mode remains enabled. Operationally, `GAME_5M_EXIT_ONLY_TAKE=false` is still cleaner because it restores normal `TIME_EXIT` behavior too.

## Phase 2: Hanger Definition v2

Replace the current binary hanger definition with a scored diagnosis:

```text
hanger_score = age_score
             + distance_to_take_score
             + weak_momentum_score
             + drawdown_score
             + missed_opportunity_score
```

Classify:

- `recoverable_hanger`: mild drawdown or small positive PnL, weak but not broken trend;
- `stale_reversal`: negative PnL, HOLD/SELL, weak momentum, age beyond expected hold;
- `normal_hold`: still young or supported by STRONG_BUY/positive momentum.

Only `recoverable_hanger` should receive lower take cap. `stale_reversal` should close.

## Phase 3: First-30/60-Minute Stuck-Risk Model

Train a simple supervised model before adding a neural net:

Target labels:

- `stuck`: no TAKE_PROFIT within N bars/days or exit negative after max hold;
- `quick_win`: TAKE_PROFIT within same day or within configured max hold;
- `bad_reversal`: drawdown exceeds threshold and no recovery.

Candidate features:

- entry RSI, momentum_2h, volatility_5m, ATR, volume_vs_avg;
- first 30/60 minute return after entry;
- MFE/MAE in first 30/60 minutes;
- current decision drift: BUY -> HOLD/SELL;
- distance to dynamic take;
- market/session phase;
- ticker-specific historical quick-win rate.

Start with CatBoost/logistic regression and only then evaluate a neural net. The dataset is tabular and limited; CatBoost is likely a better first model.

## Validation

Use replay before production:

1. Replay open and recently closed GAME_5M trades with the new stale/reversal rule.
2. Check avoided losses on NBIS/ASML-like cases.
3. Check false exits where the position later recovered to take-profit.
4. Compare:
   - realized PnL;
   - capital-days locked;
   - missed upside after early exit;
   - count of `TIME_EXIT_EARLY`.

## Rollout

1. Add code path behind config flags.
2. Enable in paper/log-only mode if needed.
3. Enable for high-risk tickers first.
4. Review closed reports after one trading week.
5. Promote to default if it reduces capital lock and large stale losses without cutting too many quick recoveries.

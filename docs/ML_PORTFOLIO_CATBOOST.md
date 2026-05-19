# Portfolio CatBoost: expected daily return

Advisory and execution-adjacent ML layer for the portfolio game. It predicts forward log-return from daily `quotes`. Training and cards are advisory; **entry filter**, **take snapshot**, and **trailing exit** are optional execution hooks (see below).

Premarket is an input context for regular-session decisions, not a separate execution mode. Portfolio BUY remains blocked outside the regular NYSE session unless `TRADING_CYCLE_ALLOW_OFFHOURS_BUY=true` is explicitly set for an emergency/manual case.

Canonical portfolio flow: [PORTFOLIO_GAME.md](PORTFOLIO_GAME.md). Cron map: [CRONS_AND_TAKE_STOP.md](CRONS_AND_TAKE_STOP.md).

## Scope

- Target universe for display: portfolio trade tickers from `get_portfolio_trade_tickers()`.
- Feature universe: portfolio tickers + `GAME_5M` tickers + `get_tickers_for_5m_correlation()` context.
- `GAME_5M` tickers are used as correlation/cross-asset context. They are not automatically treated as portfolio trade candidates unless they are also in the portfolio list.
- Leader/core labels are read from `PORTFOLIO_LEADER_CLUSTER` and `PORTFOLIO_CORE_CLUSTER` when present. Values may be comma-separated or JSON lists.

## Target

Default training target:

```text
target_log_return = log(close[t+5] / close[t])
```

The training dataset also emits `future_log_return_1d`, `future_log_return_3d`, and `future_log_return_5d` for analysis. The binary quality label is:

```text
target_good_entry = target_log_return > log(1 + (PORTFOLIO_ML_TRANSACTION_COST_BPS + PORTFOLIO_ML_MIN_EDGE_BPS) / 10000)
```

Defaults are `20` bps transaction cost and `30` bps minimum edge. This keeps transaction costs explicit and preserves the project rule to use log-returns for financial calculations.

## Features

The feature builder is `services/portfolio_ml_features.py`.

Feature groups:

- Daily price/volume: log close, returns over `1/3/5/10/20d`, volatility, gap, range, volume z-score.
- Technical context: RSI, `SMA5` distance, `volatility_5`, drawdown from 20-day high, rolling slopes.
- Universe flags: portfolio ticker, 5m ticker, leader cluster.
- Correlation context: rolling correlations to 5m basket, portfolio basket, leader/core baskets, and peer correlation aggregates.
- Relative strength: 5-day return vs 5m, portfolio, leader, and core baskets.
- Planned premarket context from `premarket_daily_features`: premarket gap vs previous close, gap vs normal volatility, minutes to open, premarket direction, premarket range/volume/VWAP, news before open, correlated-ticker premarket move, and labels for gap follow-through vs gap fade after 09:30 ET.

Premarket collection:

```bash
docker compose exec lse python scripts/ingest_premarket_daily_features.py --ensure-table
```

The table key includes `snapshot_label`, so the same morning can store several slices (`0800_ET`, `0900_ET`, `0925_ET`) for later comparison.

## Artifact paths

There is **no** `local/ml/` directory in this repo (that path was never used). CatBoost artifacts live under **`models/`** plus optional **`logs/`** for training reports.

| Environment | Model + meta | Training log (append) |
|-------------|--------------|------------------------|
| **GCP / Docker (`lse-bot`)** | `~/lse/logs/ml/models/portfolio_return_catboost.cbm` on VM; same files as `/app/logs/ml/models/...` in container | `/app/logs/ml/logs/portfolio_daily_ml_report.jsonl` |
| **Local dev** (no `/app/logs`) | `local/models/portfolio_return_catboost.cbm` (+ `.meta.json`) | `local/logs/portfolio_daily_ml_report.jsonl` |

`scripts/train_portfolio_catboost.py` picks the output path in this order:

1. `--out path.cbm` (explicit)
2. `PORTFOLIO_CATBOOST_MODEL_PATH` from `config.env` if set
3. Else `/app/logs/ml/models/portfolio_return_catboost.cbm` when `/app/logs` exists (container), else `local/models/...`

On production, `config.env` should point inference at the mounted volume, for example:

```env
PORTFOLIO_CATBOOST_MODEL_PATH=/app/logs/ml/models/portfolio_return_catboost.cbm
PORTFOLIO_ML_REPORT_JSONL=/app/logs/ml/logs/portfolio_daily_ml_report.jsonl
```

`.cbm` and `.meta.json` are **not in git** (see `.gitignore` for `local/models/*`). They persist on the VM under `logs/` because `~/lse/logs` is bind-mounted to `/app/logs` in the container.

Check models on the VM:

```bash
ls -la ~/lse/logs/ml/models/portfolio_return_catboost.*
docker exec lse-bot ls -la /app/logs/ml/models/portfolio_return_catboost.*
```

Regular retrain on prod: `scripts/run_ml_train_readiness_cron.py` (cron ~23:50 ET weekdays) may call `train_portfolio_catboost.py` when readiness gates pass; metrics also land in `logs/ml/ml_data_quality/last_portfolio_train_metrics.json` when the data-quality cron runs.

## Training

Install CatBoost dependencies:

```bash
pip install -r requirements-catboost.txt
```

Train or dry-run (from repo root; use `docker exec lse-bot` on GCP so paths resolve to `/app/logs/ml/...`):

```bash
python scripts/train_portfolio_catboost.py --dry-run
python scripts/train_portfolio_catboost.py --horizon-days 5 --min-rows 300
```

Optional explicit output (overrides defaults):

```bash
# local workstation
python scripts/train_portfolio_catboost.py --out local/models/portfolio_return_catboost.cbm

# inside lse-bot (writes to host ~/lse/logs/ml/models/ via mount)
docker exec lse-bot python scripts/train_portfolio_catboost.py --horizon-days 5 --min-rows 300
```

Validation is time-based: the last `--valid-ratio` fraction of rows by date is used as holdout. The script reports RMSE, MAE, top-decile mean forward return, and hit-rate over the transaction-cost threshold.

## Runtime inference

Runtime inference is in `services/portfolio_catboost_signal.py`.

### Config (model + cards)

```env
PORTFOLIO_CATBOOST_ENABLED=true
# Prod (lse-bot): default bind-mount path — do not use local/models here
PORTFOLIO_CATBOOST_MODEL_PATH=/app/logs/ml/models/portfolio_return_catboost.cbm
# PORTFOLIO_ML_REPORT_JSONL=/app/logs/ml/logs/portfolio_daily_ml_report.jsonl
# PORTFOLIO_ML_TRANSACTION_COST_BPS=20
# PORTFOLIO_ML_MIN_EDGE_BPS=30
```

When disabled, missing, or misconfigured, the API returns status fields and keeps portfolio cards working. When enabled and model files exist, `/api/portfolio/cards` includes:

- `portfolio_ml_expected_return_pct`
- `portfolio_ml_entry_score`
- `portfolio_ml_horizon_days`
- `portfolio_ml_cluster_role`
- `portfolio_ml_status`

### Config (execution — entry)

| Key | Default (example) | Applied in |
|-----|-------------------|------------|
| `PORTFOLIO_CATBOOST_BLOCK_BUY_ON_WEAK` | true | `services/portfolio_entry_guards.py` |
| `PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE` | 48 | skip **new** portfolio BUY if `entry_score` lower |

When `portfolio_ml_status != ok`, the block does not fire (cron falls back to strategy-only entry).

On every portfolio BUY, ML fields are merged into `trade_history.context_json` via `merge_portfolio_buy_context()`.

### Config (execution — take snapshot on entry)

| Key | Default (example) | Applied in |
|-----|-------------------|------------|
| `PORTFOLIO_ML_TAKE_ENABLED` | true | `services/portfolio_exit_policy.py` |
| `PORTFOLIO_ML_TAKE_FACTOR` | 1.5 | `max(base_take, factor × expected_return_pct)` |
| `PORTFOLIO_ML_TAKE_FLOOR_PCT` | 4 | clamp floor |
| `PORTFOLIO_ML_TAKE_CAP_PCT` | 18 | clamp cap |

Snapshot written at BUY:

```text
portfolio_effective_take_pct_at_entry
portfolio_effective_take_note
```

**Exit does not re-run CatBoost** — only the entry snapshot is used (`resolve_effective_take_pct`).

### Config (execution — trailing exit)

| Key | Default (example) | Applied in |
|-----|-------------------|------------|
| `PORTFOLIO_TRAILING_TAKE_ENABLED` | true | `trailing_take_should_close` |
| `PORTFOLIO_TRAILING_MIN_PROFIT_PCT` | 8 | arm trailing after peak P/L ≥ this |
| `PORTFOLIO_TRAILING_PULLBACK_PCT` | 3 | close if giveback from peak ≥ this |

Peak P/L uses `MAX(high)` from daily `quotes` since entry date. Checked in `ExecutionAgent.check_stop_losses()` **before** fixed take.

## Code map

| Module | Role |
|--------|------|
| `services/portfolio_catboost_signal.py` | Load model, predict, score 0–100 |
| `services/portfolio_entry_guards.py` | Block weak BUY; merge context on BUY |
| `services/portfolio_exit_policy.py` | ML take snapshot logic, trailing, `evaluate_portfolio_exit` |
| `execution_agent.py` | Calls guards on BUY, exit policy on close |
| `scripts/trading_cycle_cron.py` | Cron entrypoint |

## Tuning grid

**There is no automated replay/grid for portfolio** (unlike `services/game5m_replay_proposals.py` for GAME_5M).

Recommended manual grid (change one knob per observation window):

| Knob | Typical values | Notes |
|------|----------------|-------|
| `PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE` | 45, 48, 52 | stricter = fewer entries |
| `PORTFOLIO_ML_TAKE_FACTOR` | 1.2, 1.5, 2.0 | only matters if expected return > 0 |
| `PORTFOLIO_ML_TAKE_CAP_PCT` | 15, 18, 22 | caps ML-inflated take |
| `PORTFOLIO_TRAILING_MIN_PROFIT_PCT` | 6, 8, 10 | when trailing arms |
| `PORTFOLIO_TRAILING_PULLBACK_PCT` | 2, 3, 4 | sensitivity to pullback |

Use `/api/analyzer?strategy=PORTFOLIO` for `portfolio_catboost_status` and closed-trade stats. CatBoost **entry backtest** in the analyzer is **skipped** for `PORTFOLIO` (model is not the GAME_5M entry classifier).

## Limitations

- MVP uses daily data only. Hourly data can be added later as a separate entry-timing layer.
- Typical live `portfolio_ml_expected_return_pct` is ~1% over 5d — ML take often equals strategy take unless factor/cap are retuned after model improves.
- Validate score threshold and take knobs on walk-forward before aggressive blocking.
- Leader/core quality depends on keeping `PORTFOLIO_LEADER_CLUSTER` / `PORTFOLIO_CORE_CLUSTER` current.

# Portfolio CatBoost: expected daily return

Advisory ML layer for the portfolio game. It predicts forward log-return from daily `quotes`; it does not open or close positions by itself.

Premarket is an input context for regular-session decisions, not a separate execution mode. Portfolio BUY remains blocked outside the regular NYSE session unless `TRADING_CYCLE_ALLOW_OFFHOURS_BUY=true` is explicitly set for an emergency/manual case.

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

## Training

Install CatBoost dependencies:

```bash
pip install -r requirements-catboost.txt
```

Train or dry-run:

```bash
python scripts/train_portfolio_catboost.py --dry-run
python scripts/train_portfolio_catboost.py --horizon-days 5 --min-rows 300
```

Default output:

```text
local/models/portfolio_return_catboost.cbm
local/models/portfolio_return_catboost.meta.json
```

Validation is time-based: the last `--valid-ratio` fraction of rows by date is used as holdout. The script reports RMSE, MAE, top-decile mean forward return, and hit-rate over the transaction-cost threshold.

## Runtime

Runtime inference is in `services/portfolio_catboost_signal.py`.

Config:

```env
PORTFOLIO_CATBOOST_ENABLED=true
# PORTFOLIO_CATBOOST_MODEL_PATH=/path/to/portfolio_return_catboost.cbm
# PORTFOLIO_ML_TRANSACTION_COST_BPS=20
# PORTFOLIO_ML_MIN_EDGE_BPS=30
```

When disabled, missing, or misconfigured, the API returns status fields and keeps portfolio cards working. When enabled and model files exist, `/api/portfolio/cards` includes:

- `portfolio_ml_expected_return_pct`
- `portfolio_ml_entry_score`
- `portfolio_ml_horizon_days`
- `portfolio_ml_cluster_role`
- `portfolio_ml_status`

## Limitations

- MVP uses daily data only. Hourly data can be added later as a separate entry-timing layer.
- The score is advisory and should be validated on walk-forward windows before being used in execution rules.
- Leader/core quality depends on keeping `PORTFOLIO_LEADER_CLUSTER` / `PORTFOLIO_CORE_CLUSTER` current.

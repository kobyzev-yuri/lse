"""
Daily feature builder for the portfolio expected-return CatBoost model.

The model is advisory for portfolio game entries. 5m tickers are included in
the feature universe only as cross-asset/correlation context unless they are
also part of the portfolio trading universe.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

from config_loader import get_config_value
from report_generator import get_engine
from services.portfolio_card import get_portfolio_trade_tickers
from services.ticker_groups import get_tickers_for_5m_correlation, get_tickers_game_5m

logger = logging.getLogger(__name__)

MODEL_VERSION = "portfolio_daily_v1"
DEFAULT_HORIZON_DAYS = 5
DEFAULT_CORR_WINDOW_DAYS = 30

CATEGORICAL_FEATURE_KEYS: Tuple[str, ...] = ("ticker", "cluster_role")
NUMERIC_FEATURE_KEYS: Tuple[str, ...] = (
    "is_portfolio_ticker",
    "is_game5m_ticker",
    "is_leader_cluster",
    "close",
    "log_close",
    "ret_1d",
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "vol_5d",
    "vol_10d",
    "vol_20d",
    "sma5_distance_pct",
    "rsi",
    "volatility_5",
    "volume_z20",
    "gap_pct",
    "intraday_range_pct",
    "close_to_high20_pct",
    "drawdown_from_high20_pct",
    "trend_slope_5d",
    "trend_slope_20d",
    "corr_game_basket_30d",
    "corr_portfolio_basket_30d",
    "corr_leader_basket_30d",
    "corr_core_basket_30d",
    "rel_ret_vs_game_5d",
    "rel_ret_vs_portfolio_5d",
    "rel_ret_vs_leader_5d",
    "rel_ret_vs_core_5d",
    "corr_max_game_peer_30d",
    "corr_mean_game_peer_30d",
    "corr_max_abs_universe_peer_30d",
    "corr_mean_universe_peer_30d",
)


@dataclass(frozen=True)
class PortfolioMLUniverse:
    portfolio_tickers: List[str]
    game5m_tickers: List[str]
    correlation_tickers: List[str]
    universe_tickers: List[str]
    leaders: List[str]
    core: List[str]


def get_portfolio_ml_feature_schema() -> Tuple[List[str], List[int], List[str]]:
    """Return feature names, categorical indices, and numeric feature names."""
    colnames = list(CATEGORICAL_FEATURE_KEYS) + list(NUMERIC_FEATURE_KEYS)
    cat_idx = list(range(len(CATEGORICAL_FEATURE_KEYS)))
    return colnames, cat_idx, list(NUMERIC_FEATURE_KEYS)


def _dedupe_upper(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in items:
        t = str(raw or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _parse_ticker_list_config(key: str) -> List[str]:
    raw = (get_config_value(key, "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return _dedupe_upper(str(x) for x in parsed)
        if isinstance(parsed, dict):
            for k in ("tickers", "leaders", "core"):
                v = parsed.get(k)
                if isinstance(v, list):
                    return _dedupe_upper(str(x) for x in v)
    except Exception:
        pass
    return _dedupe_upper(x for x in raw.replace(";", ",").split(",") if x.strip())


def get_portfolio_ml_universe() -> PortfolioMLUniverse:
    """Combined universe for training/features and explicit portfolio output set."""
    portfolio = _dedupe_upper(get_portfolio_trade_tickers() or [])
    game = _dedupe_upper(get_tickers_game_5m() or [])
    corr = _dedupe_upper(get_tickers_for_5m_correlation() or [])
    leaders = _parse_ticker_list_config("PORTFOLIO_LEADER_CLUSTER")
    core = _parse_ticker_list_config("PORTFOLIO_CORE_CLUSTER")
    universe = _dedupe_upper(portfolio + game + corr + leaders + core)
    return PortfolioMLUniverse(
        portfolio_tickers=portfolio,
        game5m_tickers=game,
        correlation_tickers=corr,
        universe_tickers=universe,
        leaders=leaders,
        core=core,
    )


def portfolio_ml_threshold_log() -> float:
    """
    Minimum useful forward log-return for binary quality metrics.

    Explicit bps make transaction costs visible and keep the target independent
    of broker-specific commission fields.
    """
    try:
        cost_bps = float(get_config_value("PORTFOLIO_ML_TRANSACTION_COST_BPS", "20") or 20)
    except (TypeError, ValueError):
        cost_bps = 20.0
    try:
        edge_bps = float(get_config_value("PORTFOLIO_ML_MIN_EDGE_BPS", "30") or 30)
    except (TypeError, ValueError):
        edge_bps = 30.0
    return float(math.log1p(max(0.0, cost_bps + edge_bps) / 10000.0))


def load_daily_quotes_for_ml(tickers: List[str], *, days: Optional[int] = None) -> pd.DataFrame:
    """Load daily OHLCV/indicator rows from quotes for the ML universe."""
    tickers = _dedupe_upper(tickers)
    if not tickers:
        return pd.DataFrame()
    params: Dict[str, Any] = {f"t{i}": t for i, t in enumerate(tickers)}
    placeholders = ", ".join(f":t{i}" for i in range(len(tickers)))
    date_filter = ""
    if days is not None and int(days) > 0:
        date_filter = " AND date >= CURRENT_DATE - (:days || ' days')::interval"
        params["days"] = int(days)
    query = f"""
        SELECT date, ticker, open, high, low, close, volume, sma_5, rsi, volatility_5
        FROM quotes
        WHERE UPPER(TRIM(ticker)) IN ({placeholders})
        {date_filter}
        ORDER BY ticker ASC, date ASC
    """
    with get_engine().connect() as conn:
        df = pd.read_sql(text(query), conn, params=params)
    if df.empty:
        return df
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open", "high", "low", "close", "volume", "sma_5", "rsi", "volatility_5"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


def _rolling_slope(values: pd.Series, window: int) -> pd.Series:
    x = np.arange(window, dtype=float)
    x = x - x.mean()
    denom = float((x * x).sum())

    def _one(arr: np.ndarray) -> float:
        if len(arr) != window or not np.isfinite(arr).all() or denom <= 0:
            return 0.0
        y = arr.astype(float) - float(np.mean(arr))
        return float((x * y).sum() / denom)

    return values.rolling(window, min_periods=window).apply(_one, raw=True)


def _safe_corr(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    return a.rolling(window, min_periods=max(5, window // 2)).corr(b).replace([np.inf, -np.inf], np.nan)


def _basket_return_frame(log_ret: pd.DataFrame, tickers: List[str]) -> pd.Series:
    cols = [t for t in tickers if t in log_ret.columns]
    if not cols:
        return pd.Series(index=log_ret.index, dtype=float)
    return log_ret[cols].mean(axis=1, skipna=True)


def _cluster_role(ticker: str, leaders: set[str], core: set[str]) -> str:
    if ticker in leaders:
        return "leader"
    if ticker in core:
        return "core"
    return "unassigned"


def build_portfolio_ml_dataset(
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    corr_window_days: int = DEFAULT_CORR_WINDOW_DAYS,
    days: Optional[int] = None,
    include_targets: bool = True,
    min_history_days: int = 40,
) -> pd.DataFrame:
    """
    Build one row per ticker/date with leak-safe daily features and forward targets.

    Features at date t use data available through close[t]. Targets use future
    close[t+horizon] and are only used in offline training/evaluation.
    """
    horizon_days = max(1, int(horizon_days))
    corr_window_days = max(5, int(corr_window_days))
    universe = get_portfolio_ml_universe()
    quotes = load_daily_quotes_for_ml(universe.universe_tickers, days=days)
    if quotes.empty:
        return pd.DataFrame()

    leaders = set(universe.leaders)
    core = set(universe.core)
    portfolio_set = set(universe.portfolio_tickers)
    game_set = set(universe.game5m_tickers)

    prices = quotes.pivot_table(index="date", columns="ticker", values="close").sort_index()
    prices = prices.replace(0, np.nan)
    log_prices = np.log(prices)
    log_ret = (log_prices - log_prices.shift(1)).replace([np.inf, -np.inf], np.nan)

    game_basket = _basket_return_frame(log_ret, universe.game5m_tickers)
    portfolio_basket = _basket_return_frame(log_ret, universe.portfolio_tickers)
    leader_basket = _basket_return_frame(log_ret, universe.leaders)
    core_basket = _basket_return_frame(log_ret, universe.core)

    rows: List[pd.DataFrame] = []
    for ticker, g in quotes.groupby("ticker", sort=False):
        t = str(ticker).strip().upper()
        g = g.sort_values("date").copy()
        if len(g) < min_history_days:
            continue
        g["ticker"] = t
        g["cluster_role"] = _cluster_role(t, leaders, core)
        g["is_portfolio_ticker"] = 1.0 if t in portfolio_set else 0.0
        g["is_game5m_ticker"] = 1.0 if t in game_set else 0.0
        g["is_leader_cluster"] = 1.0 if t in leaders else 0.0

        close = g["close"].replace(0, np.nan)
        open_px = g["open"].replace(0, np.nan)
        high = g["high"].replace(0, np.nan)
        low = g["low"].replace(0, np.nan)
        volume = g["volume"].replace(0, np.nan)
        lr = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan)

        g["log_close"] = np.log(close).replace([np.inf, -np.inf], np.nan)
        for n in (1, 3, 5, 10, 20):
            g[f"ret_{n}d"] = np.log(close / close.shift(n)).replace([np.inf, -np.inf], np.nan)
        for n in (5, 10, 20):
            g[f"vol_{n}d"] = lr.rolling(n, min_periods=max(3, n // 2)).std()
        g["sma5_distance_pct"] = ((close / g["sma_5"].replace(0, np.nan)) - 1.0) * 100.0
        g["volume_z20"] = (volume - volume.rolling(20, min_periods=10).mean()) / volume.rolling(20, min_periods=10).std()
        g["gap_pct"] = ((open_px / close.shift(1)) - 1.0) * 100.0
        g["intraday_range_pct"] = ((high - low) / close) * 100.0
        high20 = high.rolling(20, min_periods=5).max()
        g["close_to_high20_pct"] = (close / high20) * 100.0
        g["drawdown_from_high20_pct"] = ((close / high20) - 1.0) * 100.0
        g["trend_slope_5d"] = _rolling_slope(g["log_close"], 5)
        g["trend_slope_20d"] = _rolling_slope(g["log_close"], 20)

        idx = pd.DatetimeIndex(g["date"])
        ticker_ret = log_ret[t].reindex(idx) if t in log_ret.columns else pd.Series(index=idx, dtype=float)
        game_ref = game_basket.reindex(idx)
        portfolio_ref = portfolio_basket.reindex(idx)
        leader_ref = leader_basket.reindex(idx)
        core_ref = core_basket.reindex(idx)
        g["corr_game_basket_30d"] = _safe_corr(ticker_ret, game_ref, corr_window_days).to_numpy()
        g["corr_portfolio_basket_30d"] = _safe_corr(ticker_ret, portfolio_ref, corr_window_days).to_numpy()
        g["corr_leader_basket_30d"] = _safe_corr(ticker_ret, leader_ref, corr_window_days).to_numpy()
        g["corr_core_basket_30d"] = _safe_corr(ticker_ret, core_ref, corr_window_days).to_numpy()

        g["rel_ret_vs_game_5d"] = g["ret_5d"] - game_ref.rolling(5, min_periods=3).sum().to_numpy()
        g["rel_ret_vs_portfolio_5d"] = g["ret_5d"] - portfolio_ref.rolling(5, min_periods=3).sum().to_numpy()
        g["rel_ret_vs_leader_5d"] = g["ret_5d"] - leader_ref.rolling(5, min_periods=3).sum().to_numpy()
        g["rel_ret_vs_core_5d"] = g["ret_5d"] - core_ref.rolling(5, min_periods=3).sum().to_numpy()

        game_corr_vals: List[pd.Series] = []
        universe_corr_vals: List[pd.Series] = []
        for other in log_ret.columns:
            if other == t:
                continue
            c = _safe_corr(ticker_ret, log_ret[other].reindex(idx), corr_window_days)
            universe_corr_vals.append(c)
            if other in game_set:
                game_corr_vals.append(c)

        def _row_agg(series_list: List[pd.Series], fn: str) -> np.ndarray:
            if not series_list:
                return np.zeros(len(g), dtype=float)
            mat = pd.concat(series_list, axis=1)
            if fn == "mean":
                return mat.mean(axis=1, skipna=True).fillna(0.0).to_numpy()
            if fn == "max":
                return mat.max(axis=1, skipna=True).fillna(0.0).to_numpy()
            if fn == "max_abs":
                return mat.abs().max(axis=1, skipna=True).fillna(0.0).to_numpy()
            return np.zeros(len(g), dtype=float)

        g["corr_max_game_peer_30d"] = _row_agg(game_corr_vals, "max")
        g["corr_mean_game_peer_30d"] = _row_agg(game_corr_vals, "mean")
        g["corr_max_abs_universe_peer_30d"] = _row_agg(universe_corr_vals, "max_abs")
        g["corr_mean_universe_peer_30d"] = _row_agg(universe_corr_vals, "mean")

        if include_targets:
            for h in (1, 3, 5, horizon_days):
                col = f"future_log_return_{h}d"
                g[col] = np.log(close.shift(-h) / close).replace([np.inf, -np.inf], np.nan)
            target_col = f"future_log_return_{horizon_days}d"
            g["target_log_return"] = g[target_col]
            thresh = portfolio_ml_threshold_log()
            g["target_good_entry"] = (g["target_log_return"] > thresh).astype(float)

        rows.append(g)

    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    keep_cols = ["date"] + list(CATEGORICAL_FEATURE_KEYS) + list(NUMERIC_FEATURE_KEYS)
    target_cols = [c for c in out.columns if c.startswith("future_log_return_")] + ["target_log_return", "target_good_entry"]
    cols = [c for c in keep_cols + target_cols if c in out.columns]
    out = out[cols].copy()
    for k in NUMERIC_FEATURE_KEYS:
        out[k] = pd.to_numeric(out[k], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["cluster_role"] = out["cluster_role"].fillna("unassigned").astype(str)
    out["ticker"] = out["ticker"].fillna("UNKNOWN").astype(str)
    return out.sort_values(["date", "ticker"]).reset_index(drop=True)


def build_latest_portfolio_ml_features(
    tickers: Optional[List[str]] = None,
    *,
    corr_window_days: int = DEFAULT_CORR_WINDOW_DAYS,
    lookback_days: int = 260,
) -> pd.DataFrame:
    """Latest feature row per requested ticker, suitable for inference."""
    df = build_portfolio_ml_dataset(
        horizon_days=DEFAULT_HORIZON_DAYS,
        corr_window_days=corr_window_days,
        days=max(lookback_days, 90),
        include_targets=False,
        min_history_days=20,
    )
    if df.empty:
        return df
    wanted = _dedupe_upper(tickers or get_portfolio_ml_universe().portfolio_tickers)
    if wanted:
        df = df[df["ticker"].isin(wanted)]
    if df.empty:
        return df
    idx = df.groupby("ticker")["date"].idxmax()
    return df.loc[idx].sort_values("ticker").reset_index(drop=True)


def feature_frame_to_rows(df: pd.DataFrame) -> Tuple[List[str], List[int], List[List[Any]]]:
    """Convert a feature dataframe to CatBoost row order."""
    colnames, cat_idx, _ = get_portfolio_ml_feature_schema()
    rows: List[List[Any]] = []
    for _, r in df.iterrows():
        row: List[Any] = []
        for c in colnames:
            if c in CATEGORICAL_FEATURE_KEYS:
                row.append(str(r.get(c) or "unassigned"))
            else:
                try:
                    x = float(r.get(c, 0.0))
                    row.append(x if math.isfinite(x) else 0.0)
                except (TypeError, ValueError):
                    row.append(0.0)
        rows.append(row)
    return colnames, cat_idx, rows

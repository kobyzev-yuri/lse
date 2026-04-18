"""
Реплей выходов GAME_5M по OHLC барам (5m и 30m) для одних и тех же входов из trade_history.

Импульс «2ч» на 30m: те же ~120 минут стеной времени → 4 баров по 30m (см. BARS_2H_30M).
Тейк/стоп — через game_5m.should_close_position с simulation_time = момент закрытия бара.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
import copy
from datetime import time as dt_time
from datetime import timedelta
from typing import Any, Callable, Dict, List, Literal, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from services.game_5m import _effective_take_profit_pct, should_close_position, trade_ts_to_et
from services.recommend_5m import (
    GAME_5M_RULE_VERSION,
    RSI_PERIOD_5M,
    compute_30m_features,
    compute_5m_features,
    compute_rsi_5m,
    decide_game5m_technical,
    fetch_30m_ohlc,
    fetch_5m_ohlc,
    get_decision_5m_rule_thresholds,
)

logger = logging.getLogger(__name__)


def momentum_2h_pct_from_closes(closes: pd.Series, bars_2h: int) -> Optional[float]:
    """Процентное изменение за последние `bars_2h` интервалов (как momentum_2h_pct в compute_5m_features)."""
    if closes is None or closes.empty:
        return None
    s = closes.astype(float).reset_index(drop=True)
    n = min(int(bars_2h), len(s) - 1)
    if n < 1:
        return None
    price = float(s.iloc[-1])
    price_ago = float(s.iloc[-(n + 1)])
    if price_ago <= 0:
        return None
    return ((price / price_ago) - 1.0) * 100.0


def _normalize_df_datetime_et(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "datetime" not in df.columns:
        return df
    out = df.copy()
    d = pd.to_datetime(out["datetime"])
    if d.dt.tz is None:
        try:
            d = d.dt.tz_localize("America/New_York", ambiguous=True)
        except Exception:
            d = d.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
    else:
        d = d.dt.tz_convert("America/New_York")
    out["datetime"] = d
    return out.sort_values("datetime").reset_index(drop=True)


def read_market_bars_range(
    engine: Engine,
    *,
    table: Literal["market_bars_5m", "market_bars_30m"],
    symbol: str,
    exchange: str,
    start_utc: pd.Timestamp,
    end_utc: pd.Timestamp,
) -> pd.DataFrame:
    """Читает бары из Postgres; datetime в колонке — bar open в ET (как yfinance после нормализации)."""
    q = text(
        f"""
        SELECT bar_start_utc AS datetime, open AS "Open", high AS "High",
               low AS "Low", close AS "Close", volume AS "Volume"
        FROM {table}
        WHERE exchange = :ex AND symbol = :sym
          AND bar_start_utc >= :t0 AND bar_start_utc <= :t1
        ORDER BY bar_start_utc ASC
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"ex": exchange, "sym": symbol, "t0": start_utc, "t1": end_utc})
    if df is None or df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("America/New_York")
    for c in ("Open", "High", "Low", "Close"):
        if c in df.columns:
            df[c] = df[c].astype(float)
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
    return df.reset_index(drop=True)


@dataclass
class ReplayExit:
    bar_idx: int
    bar_open_et: pd.Timestamp
    bar_end_et: pd.Timestamp
    signal_type: str
    exit_detail: str
    exit_fill_price: float
    momentum_2h_pct: Optional[float]
    take_pct_effective: Optional[float]


def _bar_end_et(bar_open: pd.Timestamp, minutes: int) -> pd.Timestamp:
    t = pd.Timestamp(bar_open)
    if t.tzinfo is None:
        t = t.tz_localize("America/New_York", ambiguous=True)
    return t + pd.Timedelta(minutes=minutes)


def _exit_fill_price(
    signal_type: str,
    close: float,
    high: float,
    low: float,
) -> float:
    if signal_type == "TAKE_PROFIT":
        return max(close, high)
    if signal_type == "STOP_LOSS":
        return min(close, low)
    return float(close)


def replay_game5m_on_bars(
    df: pd.DataFrame,
    *,
    entry_ts_et: pd.Timestamp,
    entry_price: float,
    ticker: str,
    bar_minutes: int,
    momentum_fn: Callable[[pd.DataFrame], Optional[float]],
) -> Optional[ReplayExit]:
    """
    Идём по барам после входа; на каждом закрытии считаем импульс через momentum_fn(slice),
    вызываем should_close_position(..., simulation_time=bar_end_et).
    """
    if df is None or df.empty or entry_price <= 0:
        return None
    df = _normalize_df_datetime_et(df)
    entry_ts_et = pd.Timestamp(trade_ts_to_et(entry_ts_et))
    if entry_ts_et.tzinfo is None:
        entry_ts_et = entry_ts_et.tz_localize("America/New_York", ambiguous=True)
    else:
        entry_ts_et = entry_ts_et.tz_convert("America/New_York")

    open_pos: dict[str, Any] = {
        "entry_price": float(entry_price),
        "entry_ts": entry_ts_et.to_pydatetime(),
        "ticker": ticker,
    }

    for i in range(len(df)):
        row = df.iloc[i]
        bar_open = pd.Timestamp(row["datetime"])
        bar_end = _bar_end_et(bar_open, bar_minutes)
        if bar_end <= entry_ts_et:
            continue

        slice_df = df.iloc[: i + 1].copy()
        mom = momentum_fn(slice_df)
        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        should, sig, det = should_close_position(
            open_pos,
            "HOLD",
            close,
            mom,
            bar_high=high,
            bar_low=low,
            simulation_time=bar_end.to_pydatetime(),
        )
        if should and sig:
            tp_eff = _effective_take_profit_pct(mom, ticker=ticker)
            fill = _exit_fill_price(sig, close, high, low)
            return ReplayExit(
                bar_idx=i,
                bar_open_et=bar_open,
                bar_end_et=bar_end,
                signal_type=sig,
                exit_detail=det or "",
                exit_fill_price=fill,
                momentum_2h_pct=mom,
                take_pct_effective=tp_eff,
            )
    return None


def momentum_from_5m_slice(slice_df: pd.DataFrame, ticker: str = "") -> Optional[float]:
    feats = compute_5m_features(slice_df, ticker=ticker or "")
    if not feats:
        return None
    return feats.get("momentum_2h_pct")


def momentum_from_30m_slice(slice_df: pd.DataFrame, ticker: str = "") -> Optional[float]:
    feats = compute_30m_features(slice_df, ticker=ticker or "")
    if not feats:
        return None
    return feats.get("momentum_2h_pct")


def _filter_df_time(df: pd.DataFrame, t0: pd.Timestamp, t1: pd.Timestamp) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d0 = pd.Timestamp(t0).tz_convert("America/New_York")
    d1 = pd.Timestamp(t1).tz_convert("America/New_York")
    out = _normalize_df_datetime_et(df)
    m = (out["datetime"] >= d0) & (out["datetime"] <= d1)
    return out.loc[m].reset_index(drop=True)


def load_bars_5m_for_replay(
    engine: Optional[Engine],
    ticker: str,
    exchange: str,
    start_utc: pd.Timestamp,
    end_utc: pd.Timestamp,
    *,
    yfinance_days: int = 7,
) -> pd.DataFrame:
    if engine is not None:
        df = read_market_bars_range(engine, table="market_bars_5m", symbol=ticker, exchange=exchange, start_utc=start_utc, end_utc=end_utc)
        if not df.empty:
            return df
    raw = fetch_5m_ohlc(ticker, days=yfinance_days)
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _filter_df_time(_normalize_df_datetime_et(raw), start_utc, end_utc)


def load_bars_30m_for_replay(
    engine: Optional[Engine],
    ticker: str,
    exchange: str,
    start_utc: pd.Timestamp,
    end_utc: pd.Timestamp,
    *,
    yfinance_days: int = 7,
) -> pd.DataFrame:
    if engine is not None:
        df = read_market_bars_range(engine, table="market_bars_30m", symbol=ticker, exchange=exchange, start_utc=start_utc, end_utc=end_utc)
        if not df.empty:
            return df
    raw = fetch_30m_ohlc(ticker, days=yfinance_days)
    if raw is None or raw.empty:
        return pd.DataFrame()
    return _filter_df_time(_normalize_df_datetime_et(raw), start_utc, end_utc)


def log_return_pnl(entry: float, exit_px: float) -> Optional[float]:
    if entry <= 0 or exit_px <= 0:
        return None
    import numpy as np

    return float(np.log(exit_px / entry))


def min_session_bars_equivalent_30m(momentum_min_session_bars_5m: int) -> int:
    """Порог GAME_5M_MOMENTUM_MIN_SESSION_BARS задан в числе 5m-баров; для 30m — эквивалент по времени (округление вверх)."""
    try:
        n5 = int(momentum_min_session_bars_5m)
    except (TypeError, ValueError):
        n5 = 7
    return max(1, (max(1, n5) * 5 + 29) // 30)


def bar_end_in_us_rth_weekday(bar_end_et: pd.Timestamp) -> bool:
    """Закрытие бара в обычной сессии NYSE (пн–пт, 9:30–16:00 ET). Праздники биржи не вычитаем."""
    t = pd.Timestamp(bar_end_et)
    if t.tzinfo is None:
        t = t.tz_localize("America/New_York", ambiguous=True)
    else:
        t = t.tz_convert("America/New_York")
    if t.weekday() >= 5:
        return False
    tt = t.time()
    return dt_time(9, 30) <= tt <= dt_time(16, 0)


def _base_decision_rule_params_30m_sim(th: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rule_version": GAME_5M_RULE_VERSION,
        "source_fn": "services.game_5m_take_replay.simulate_game5m_30m_strategy_on_window",
        "rsi_strong_buy_max": float(th["rsi_strong_buy_max"]),
        "momentum_for_strong_buy_min": float(th["momentum_for_strong_buy_min"]),
        "rsi_buy_max": float(th["rsi_buy_max"]),
        "price_to_low5d_multiplier_max": float(th["price_to_low5d_multiplier_max"]),
        "rsi_sell_min": float(th["rsi_sell_min"]),
        "rsi_hold_overbought_min": float(th["rsi_hold_overbought_min"]),
        "momentum_buy_min": float(th["momentum_buy_min"]),
        "rsi_for_momentum_buy_max": float(th["rsi_for_momentum_buy_max"]),
        "volatility_warn_buy_min": float(th["volatility_warn_buy_min"]),
        "volatility_wait_min": float(th["volatility_wait_min"]),
        "sell_confirm_bars": int(th["sell_confirm_bars"]),
        "news_negative_min": 0.4,
        "news_very_negative_min": 0.35,
        "news_positive_min": 0.65,
        "momentum_min_session_bars_config_5m": int(th["momentum_min_session_bars"]),
        "momentum_allow_cross_day_buy": th.get("momentum_allow_cross_day_buy", False),
        "early_use_premarket_momentum": False,
        "premarket_momentum_buy_min": float(th["premarket_momentum_buy_min"]),
        "premarket_momentum_block_below": float(th["premarket_momentum_block_below"]),
    }


def simulate_game5m_30m_strategy_on_window(
    df30: pd.DataFrame,
    ticker: str,
    *,
    window_start_et: pd.Timestamp,
    window_end_et: pd.Timestamp,
    bar_minutes: int = 30,
) -> List[Dict[str, Any]]:
    """
    Эмуляция «реальной» 30m-стратегии GAME_5M на интервале: свои входы (decide_game5m_technical
    по compute_30m_features) и выходы (should_close_position по импульсу с 30m).
    Премаркет-1m не используется (early_use_premarket_mom=False). Учёт **KB/sentiment** как в
    `get_decision_5m` здесь намеренно не подключён — только техническое ядро; KB в проекте есть
    и может быть добавлен в следующей версии для сопоставимости с кроном.
    """
    df30 = _normalize_df_datetime_et(df30)
    if df30.empty:
        return []

    w0 = pd.Timestamp(window_start_et)
    w1 = pd.Timestamp(window_end_et)
    if w0.tzinfo is None:
        w0 = w0.tz_localize("America/New_York", ambiguous=True)
    else:
        w0 = w0.tz_convert("America/New_York")
    if w1.tzinfo is None:
        w1 = w1.tz_localize("America/New_York", ambiguous=True)
    else:
        w1 = w1.tz_convert("America/New_York")

    th = get_decision_5m_rule_thresholds()
    min_sess_30m = min_session_bars_equivalent_30m(int(th["momentum_min_session_bars"]))
    sell_confirm_bars = int(th["sell_confirm_bars"])

    trades: List[Dict[str, Any]] = []
    pos: Optional[Dict[str, Any]] = None

    for i in range(len(df30)):
        row = df30.iloc[i]
        bar_open = pd.Timestamp(row["datetime"])
        bar_end = _bar_end_et(bar_open, bar_minutes)
        if bar_end < w0 or bar_end > w1:
            continue

        slice_df = df30.iloc[: i + 1].copy()
        feats = compute_30m_features(slice_df, ticker)
        if not feats:
            continue

        closes = slice_df["Close"].astype(float)
        rsi_prev_values: List[float] = []
        for back in range(1, sell_confirm_bars + 1):
            if len(closes) > back + RSI_PERIOD_5M:
                rv = compute_rsi_5m(closes.iloc[: -back], period=RSI_PERIOD_5M)
                if rv is not None:
                    rsi_prev_values.append(float(rv))

        drp = copy.deepcopy(_base_decision_rule_params_30m_sim(th))
        from config_loader import get_config_value as _gcv

        allow_cross = (_gcv("GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        drp["momentum_allow_cross_day_buy"] = allow_cross

        decision, _reasons, branch, _down = decide_game5m_technical(
            ticker=ticker,
            features=feats,
            closes=closes,
            th=th,
            rsi_prev_values=rsi_prev_values,
            decision_rule_params=drp,
            min_session_bars=min_sess_30m,
            premarket_intraday_momentum_pct=None,
            early_use_premarket_mom=False,
        )

        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        if pos is not None:
            mom = feats.get("momentum_2h_pct")
            open_pos = {
                "entry_price": float(pos["entry_price"]),
                "entry_ts": pos["entry_ts"],
                "ticker": ticker,
            }
            should, sig, det = should_close_position(
                open_pos,
                "HOLD",
                close,
                mom,
                bar_high=high,
                bar_low=low,
                simulation_time=bar_end.to_pydatetime(),
            )
            if should and sig:
                fill = _exit_fill_price(sig, close, high, low)
                trades.append(
                    {
                        "ticker": ticker,
                        "entry_ts": pos["entry_ts"].isoformat() if hasattr(pos["entry_ts"], "isoformat") else str(pos["entry_ts"]),
                        "entry_price": float(pos["entry_price"]),
                        "entry_decision": pos.get("entry_decision"),
                        "entry_branch": pos.get("entry_branch"),
                        "exit_ts": bar_end.isoformat(),
                        "exit_signal": sig,
                        "exit_detail": det,
                        "exit_fill_price": round(fill, 6),
                        "log_ret": log_return_pnl(float(pos["entry_price"]), fill),
                        "timeframe": "30m_sim",
                        "min_session_bars_30m_effective": min_sess_30m,
                    }
                )
                pos = None

        if pos is None and bar_end_in_us_rth_weekday(bar_end) and decision in ("BUY", "STRONG_BUY"):
            pos = {
                "entry_price": close,
                "entry_ts": bar_end.to_pydatetime(),
                "entry_decision": decision,
                "entry_branch": branch,
            }

    return trades

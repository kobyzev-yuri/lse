"""Decision-time context features for GAME_5M entry/hold ML (news, calendar, macro, state)."""
from __future__ import annotations

import math
from datetime import time as dt_time
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

from services.cluster_recommend import CORRELATION_CB_FEATURE_KEYS

# News + calendar + macro scalars at decision bar (beyond technical BAR_TRAIN keys).
ENTRY_CONTEXT_NUMERIC_KEYS: tuple[str, ...] = (
    "kb_news_impact_enc",
    "kb_news_sentiment_mean",
    "kb_news_count",
    "session_phase_enc",
    "dow_et",
    "hour_et",
    "ndx_gap_pct",
    "spy_gap_pct",
    "premarket_gap_pct",
    "macro_risk_enc",
    "prob_up",
    "prob_down",
    "llm_sentiment",
) + CORRELATION_CB_FEATURE_KEYS

# Exit-time technical at hold bar (prefix-free for CatBoost readability).
HOLD_EXIT_TECH_KEYS: tuple[str, ...] = (
    "rsi_5m",
    "momentum_2h_pct",
    "volatility_5m_pct",
    "pullback_from_high_pct",
)

# Entry snapshot from BUY context_json (frozen at entry).
HOLD_ENTRY_SNAPSHOT_KEYS: tuple[str, ...] = (
    "entry_rsi_5m",
    "entry_vol_5m_pct",
    "entry_momentum_2h_pct",
    "entry_kb_news_impact_enc",
    "entry_prob_up",
    "entry_prob_down",
    "entry_macro_risk_enc",
)

# State on hold bar (recovery legacy subset + pnl).
HOLD_STATE_KEYS: tuple[str, ...] = (
    "pnl_pct",
    "hold_minutes",
    "minutes_after_rth_open",
    "dow",
    "hour_et",
)

# Hold-bar exit context at bar t (no dow_et/hour_et — already in HOLD_STATE_KEYS).
HOLD_EXIT_CONTEXT_NUMERIC_KEYS: tuple[str, ...] = tuple(
    k for k in ENTRY_CONTEXT_NUMERIC_KEYS if k not in ("dow_et", "hour_et")
)

# Full hold-bar tabular row for bake-off B2.
HOLD_BAR_TRAIN_NUMERIC_KEYS: tuple[str, ...] = (
    HOLD_STATE_KEYS
    + HOLD_ENTRY_SNAPSHOT_KEYS
    + HOLD_EXIT_TECH_KEYS
    + HOLD_EXIT_CONTEXT_NUMERIC_KEYS
)

_SESSION_PHASE_ORDER = (
    "PRE_MARKET",
    "NEAR_OPEN",
    "REGULAR",
    "NEAR_CLOSE",
    "AFTER_HOURS",
    "WEEKEND",
    "HOLIDAY",
)

_MACRO_RISK_ORDER = ("LOW", "MEDIUM", "HIGH", "EXTREME", "NEUTRAL", "CAUTION", "AVOID", "UNKNOWN")


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return default


def session_phase_enc_from_ts(bar_ts_et: pd.Timestamp) -> float:
    """Session phase encoded 0..6 from bar timestamp (no live market_session call)."""
    ts = bar_ts_et
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York", ambiguous=True)
    else:
        ts = ts.tz_convert("America/New_York")
    if ts.dayofweek >= 5:
        return float(_SESSION_PHASE_ORDER.index("WEEKEND"))
    t = ts.time()
    open_t = dt_time(9, 30)
    close_t = dt_time(16, 0)
    near_open_end = dt_time(10, 0)
    near_close_start = dt_time(15, 30)
    if t < open_t:
        return float(_SESSION_PHASE_ORDER.index("PRE_MARKET"))
    if t > close_t:
        return float(_SESSION_PHASE_ORDER.index("AFTER_HOURS"))
    if t <= near_open_end:
        return float(_SESSION_PHASE_ORDER.index("NEAR_OPEN"))
    if t >= near_close_start:
        return float(_SESSION_PHASE_ORDER.index("NEAR_CLOSE"))
    return float(_SESSION_PHASE_ORDER.index("REGULAR"))


def kb_news_impact_enc(impact: Any) -> float:
    s = str(impact or "").strip().lower()
    if not s or s == "нейтрально":
        return 0.0
    if "позитив" in s:
        return 1.0 if "поддерж" not in s else 1.5
    if "негатив" in s:
        if "отлож" in s or "vix" in s:
            return -1.5
        return -1.0
    return 0.0


def macro_risk_enc(level: Any) -> float:
    s = str(level or "").strip().upper()
    if not s:
        return float(_MACRO_RISK_ORDER.index("UNKNOWN"))
    if s in _MACRO_RISK_ORDER:
        return float(_MACRO_RISK_ORDER.index(s))
    return float(_MACRO_RISK_ORDER.index("UNKNOWN"))


def kb_news_stats(kb_news: list[dict[str, Any]]) -> dict[str, float]:
    scores: list[float] = []
    for n in (kb_news or [])[:10]:
        sc = n.get("sentiment_score")
        if sc is None:
            continue
        try:
            scores.append(float(sc))
        except (TypeError, ValueError):
            continue
    mean = sum(scores) / len(scores) if scores else 0.0
    return {
        "kb_news_sentiment_mean": mean,
        "kb_news_count": float(len(kb_news or [])),
    }


def prob_direction_from_technical(
    *,
    rsi_5m: Any = None,
    momentum_2h_pct: Any = None,
    volatility_5m_pct: Any = None,
) -> tuple[float, float]:
    """Heuristic prob_up/prob_down (same scoring as recommend_5m scan)."""
    up_score = 1.0
    down_score = 1.0
    try:
        rsi_now = float(rsi_5m) if rsi_5m is not None else None
    except (TypeError, ValueError):
        rsi_now = None
    try:
        mom_2h = float(momentum_2h_pct) if momentum_2h_pct is not None else None
    except (TypeError, ValueError):
        mom_2h = None
    try:
        vol_5m = float(volatility_5m_pct) if volatility_5m_pct is not None else None
    except (TypeError, ValueError):
        vol_5m = None

    if isinstance(rsi_now, (int, float)):
        if rsi_now <= 30:
            up_score += 0.6
        elif rsi_now >= 70:
            down_score += 0.6
    if isinstance(mom_2h, (int, float)):
        if mom_2h >= 1.0:
            up_score += 0.4
        elif mom_2h <= -1.0:
            down_score += 0.4
    if isinstance(vol_5m, (int, float)) and vol_5m >= 0.8:
        up_score = 1.0 + (up_score - 1.0) * 0.7
        down_score = 1.0 + (down_score - 1.0) * 0.7
    s = up_score + down_score
    return round(up_score / s, 2), round(down_score / s, 2)


def _premarket_gap_sql(
    conn: Any,
    *,
    symbol: str,
    trade_date: Any,
) -> float:
    from sqlalchemy import text

    row = conn.execute(
        text(
            """
            SELECT premarket_gap_pct
            FROM premarket_daily_features
            WHERE exchange = 'US'
              AND snapshot_label = 'latest'
              AND symbol = :sym
              AND trade_date = :d
            LIMIT 1
            """
        ),
        {"sym": str(symbol or "").strip().upper(), "d": pd.Timestamp(trade_date).date()},
    ).fetchone()
    return _safe_float(row[0]) if row else 0.0


def _index_gaps_for_date(
    conn: Any,
    trade_date: Any,
    *,
    index_cache: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    from services.ticker_groups import get_market_ndx_fallback_ticker, get_market_ndx_ticker

    dkey = str(pd.Timestamp(trade_date).date())
    if index_cache is not None and dkey in index_cache:
        return index_cache[dkey]
    ndx_sym = get_market_ndx_ticker()
    ndx_fb = get_market_ndx_fallback_ticker()
    spy_gap = _premarket_gap_sql(conn, symbol="SPY", trade_date=trade_date)
    ndx_gap = _premarket_gap_sql(conn, symbol=ndx_sym, trade_date=trade_date)
    if ndx_gap == 0.0 and ndx_fb.upper() != ndx_sym.upper():
        ndx_gap = _premarket_gap_sql(conn, symbol=ndx_fb, trade_date=trade_date)
    out = {"spy_gap_pct": spy_gap, "ndx_gap_pct": ndx_gap}
    if index_cache is not None:
        index_cache[dkey] = out
    return out


def correlation_matrix_before_date(
    engine: Any,
    tickers: list[str],
    *,
    as_of_date: Any,
    days: int = 30,
) -> dict[str, dict[str, float]] | None:
    """Daily log-return correlation using quotes strictly before as_of_date (no same-day leak)."""
    if engine is None or len(tickers) < 2:
        return None
    from sqlalchemy import text

    as_of = pd.Timestamp(as_of_date).date()
    syms = sorted({str(t).strip().upper() for t in tickers if t})
    if len(syms) < 2:
        return None
    try:
        with engine.connect() as conn:
            df = pd.read_sql(
                text(
                    """
                    SELECT ticker, date::date AS d, close::float AS close
                    FROM quotes
                    WHERE ticker = ANY(:tickers) AND date::date < :as_of
                    ORDER BY d
                    """
                ),
                conn,
                params={"tickers": syms, "as_of": as_of},
            )
    except Exception:
        return None
    if df is None or df.empty:
        return None
    pt = df.pivot_table(index="d", columns="ticker", values="close").sort_index()
    pt = pt.replace(0, np.nan).tail(max(int(days) + 5, 35))
    log_ret = np.log(pt / pt.shift(1)).replace([np.inf, -np.inf], np.nan)
    log_ret = log_ret.tail(int(days))
    if log_ret.shape[0] < 5 or log_ret.shape[1] < 2:
        return None
    corr = log_ret.corr(min_periods=5)
    if corr is None or corr.empty:
        return None
    return corr.to_dict()


def fetch_correlation_features_as_of(
    engine: Any,
    *,
    ticker: str,
    as_of_date: Any,
    days: int = 30,
    cache: dict[str, Any] | None = None,
) -> dict[str, float]:
    from services.cluster_recommend import extract_correlation_features_for_5m_entry
    from services.ticker_groups import get_tickers_for_5m_correlation

    zeros = {k: 0.0 for k in CORRELATION_CB_FEATURE_KEYS}
    dkey = str(pd.Timestamp(as_of_date).date())
    matrix: dict[str, dict[str, float]] | None = None
    if cache is not None and dkey in cache:
        matrix = cache[dkey]
    else:
        universe = list(get_tickers_for_5m_correlation() or [])
        matrix = correlation_matrix_before_date(
            engine, universe, as_of_date=as_of_date, days=days,
        )
        if cache is not None:
            cache[dkey] = matrix
    if not matrix:
        return zeros
    return extract_correlation_features_for_5m_entry(ticker, matrix)


def _gap_from_macro_json(macro_json: Any, symbol: str) -> float:
    if not macro_json:
        return 0.0
    if isinstance(macro_json, str):
        try:
            import json

            macro_json = json.loads(macro_json)
        except Exception:
            return 0.0
    if not isinstance(macro_json, dict):
        return 0.0
    info = macro_json.get(symbol) or macro_json.get(str(symbol).upper())
    if not isinstance(info, dict):
        return 0.0
    return _safe_float(info.get("gap_pct"))


def infer_kb_news_impact_label(kb_news: list[dict[str, Any]]) -> str:
    """Static impact label from KB list (no decision mutation)."""
    news_with_sentiment = [
        (n, float(n["sentiment_score"]))
        for n in (kb_news or [])[:10]
        if n.get("sentiment_score") is not None
    ]
    very_negative = [n for n, s in news_with_sentiment if s < 0.35]
    recent_negative = [n for n, s in news_with_sentiment if s < 0.4]
    recent_positive = [n for n, s in news_with_sentiment if s > 0.65]
    if very_negative:
        return "негатив (сильный)"
    if recent_negative:
        return "негатив"
    if recent_positive:
        return "позитив"
    return "нейтрально"


def fetch_calendar_gaps_as_of(
    engine: Any,
    *,
    ticker: str,
    trade_date: Any,
    cache: dict[tuple[str, str], dict[str, float]] | None = None,
    index_cache: dict[str, dict[str, float]] | None = None,
) -> dict[str, float]:
    """Premarket gaps + macro risk for trade_date (premarket_daily_features + gap_forecast)."""
    sym = str(ticker or "").strip().upper()
    dkey = str(pd.Timestamp(trade_date).date())
    ck = (sym, dkey)
    if cache is not None and ck in cache:
        return cache[ck]
    out: dict[str, float] = {
        "ndx_gap_pct": 0.0,
        "spy_gap_pct": 0.0,
        "premarket_gap_pct": 0.0,
        "macro_risk_level": "",
        "macro_risk_enc": macro_risk_enc(None),
    }
    if engine is None:
        return out
    try:
        from sqlalchemy import text

        d = pd.Timestamp(trade_date).date()
        with engine.connect() as conn:
            out["premarket_gap_pct"] = _premarket_gap_sql(conn, symbol=sym, trade_date=d)
            idx = _index_gaps_for_date(conn, d, index_cache=index_cache)
            out["ndx_gap_pct"] = idx["ndx_gap_pct"]
            out["spy_gap_pct"] = idx["spy_gap_pct"]
            gf = conn.execute(
                text(
                    """
                    SELECT macro_risk_level, premarket_gap_pct, macro_indicators_json
                    FROM game5m_gap_forecast_daily
                    WHERE symbol = :sym AND trade_date = :d
                    LIMIT 1
                    """
                ),
                {"sym": sym, "d": d},
            ).fetchone()
        if gf:
            macro_level = str(gf[0] or "").strip().upper()
            if macro_level:
                out["macro_risk_level"] = macro_level
                out["macro_risk_enc"] = macro_risk_enc(macro_level)
            if out["premarket_gap_pct"] == 0.0:
                out["premarket_gap_pct"] = _safe_float(gf[1])
            macro_json = gf[2] if len(gf) > 2 else None
            if out["spy_gap_pct"] == 0.0:
                out["spy_gap_pct"] = _gap_from_macro_json(macro_json, "SPY")
            if out["ndx_gap_pct"] == 0.0:
                from services.ticker_groups import get_market_ndx_ticker

                ndx_sym = get_market_ndx_ticker()
                out["ndx_gap_pct"] = _gap_from_macro_json(macro_json, ndx_sym)
                if out["ndx_gap_pct"] == 0.0:
                    out["ndx_gap_pct"] = idx["ndx_gap_pct"]
    except Exception:
        pass
    if cache is not None:
        cache[ck] = out
    return out


def build_entry_context_features(
    *,
    ticker: str,
    bar_ts_et: str | pd.Timestamp,
    features: Optional[Mapping[str, Any]] = None,
    entry_context: Optional[Mapping[str, Any]] = None,
    engine: Any = None,
    kb_days: int = 7,
    kb_news: Optional[list[dict[str, Any]]] = None,
    gaps_cache: dict[tuple[str, str], dict[str, float]] | None = None,
    index_gaps_cache: dict[str, dict[str, float]] | None = None,
    correlation_cache: dict[str, Any] | None = None,
) -> dict[str, float]:
    """
    Numeric context at entry/hold decision bar.
    KB filtered as_of bar close; entry_context supplies prob_up/corr when replaying hold bars.
    """
    ts = pd.Timestamp(bar_ts_et)
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York", ambiguous=True)
    else:
        ts = ts.tz_convert("America/New_York")

    if kb_news is None:
        from services.recommend_5m import fetch_kb_news_for_period

        as_of_utc = ts.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
        kb_news = fetch_kb_news_for_period(
            ticker, int(kb_days), as_of=as_of_utc, engine=engine,
        )

    impact_label = infer_kb_news_impact_label(kb_news)
    stats = kb_news_stats(kb_news)
    gaps = fetch_calendar_gaps_as_of(
        engine,
        ticker=ticker,
        trade_date=ts.date(),
        cache=gaps_cache,
        index_cache=index_gaps_cache,
    )

    ctx = dict(entry_context or {})
    feat = dict(features or {})

    macro_level = ctx.get("macro_risk_level") or feat.get("macro_risk_level") or gaps.get("macro_risk_level")
    prob_up = _safe_float(ctx.get("prob_up") or feat.get("prob_up"))
    prob_down = _safe_float(ctx.get("prob_down") or feat.get("prob_down"))
    if prob_up == 0.0 and prob_down == 0.0:
        prob_up, prob_down = prob_direction_from_technical(
            rsi_5m=feat.get("rsi_5m"),
            momentum_2h_pct=feat.get("momentum_2h_pct"),
            volatility_5m_pct=feat.get("volatility_5m_pct"),
        )

    out: dict[str, float] = {
        "kb_news_impact_enc": kb_news_impact_enc(impact_label),
        "kb_news_sentiment_mean": stats["kb_news_sentiment_mean"],
        "kb_news_count": stats["kb_news_count"],
        "session_phase_enc": session_phase_enc_from_ts(ts),
        "dow_et": float(ts.dayofweek),
        "hour_et": float(ts.hour),
        "ndx_gap_pct": gaps["ndx_gap_pct"] or _safe_float(ctx.get("ndx_gap_pct")),
        "spy_gap_pct": gaps["spy_gap_pct"] or _safe_float(ctx.get("spy_gap_pct")),
        "premarket_gap_pct": gaps["premarket_gap_pct"] or _safe_float(ctx.get("premarket_gap_pct")),
        "macro_risk_enc": macro_risk_enc(macro_level) if macro_level else gaps["macro_risk_enc"],
        "prob_up": prob_up,
        "prob_down": prob_down,
        "llm_sentiment": _safe_float(ctx.get("llm_sentiment") or feat.get("llm_sentiment")),
    }
    if any(k in ctx for k in CORRELATION_CB_FEATURE_KEYS):
        for k in CORRELATION_CB_FEATURE_KEYS:
            out[k] = _safe_float(ctx.get(k))
    elif engine is not None:
        corr_feats = fetch_correlation_features_as_of(
            engine,
            ticker=ticker,
            as_of_date=ts.date(),
            cache=correlation_cache,
        )
        out.update(corr_feats)
    else:
        for k in CORRELATION_CB_FEATURE_KEYS:
            out[k] = 0.0
    return out


def entry_snapshot_from_context(ctx: Optional[Mapping[str, Any]]) -> dict[str, float]:
    """Frozen entry fields from BUY context_json for hold-bar rows."""
    from services.deal_params_5m import normalize_entry_context

    n = normalize_entry_context(dict(ctx or {}))
    return {
        "entry_rsi_5m": _safe_float(n.get("rsi_5m")),
        "entry_vol_5m_pct": _safe_float(n.get("volatility_5m_pct")),
        "entry_momentum_2h_pct": _safe_float(n.get("momentum_2h_pct") or n.get("entry_impulse_pct")),
        "entry_kb_news_impact_enc": kb_news_impact_enc(n.get("kb_news_impact")),
        "entry_prob_up": _safe_float(n.get("prob_up")),
        "entry_prob_down": _safe_float(n.get("prob_down")),
        "entry_macro_risk_enc": macro_risk_enc(n.get("macro_risk_level")),
    }


def hold_state_features(
    *,
    entry_price: float,
    entry_ts_et: pd.Timestamp,
    bar_ts_et: pd.Timestamp,
    ref_close: float,
) -> dict[str, float]:
    pnl_pct = (ref_close / entry_price - 1.0) * 100.0 if entry_price > 0 else 0.0
    hold_min = float((bar_ts_et - entry_ts_et) / pd.Timedelta(minutes=1))
    day = bar_ts_et.normalize()
    open_et = day + pd.Timedelta(hours=9, minutes=30)
    mar = float((bar_ts_et - open_et) / pd.Timedelta(minutes=1))
    return {
        "pnl_pct": pnl_pct,
        "hold_minutes": hold_min,
        "minutes_after_rth_open": mar,
        "dow": float(bar_ts_et.dayofweek),
        "hour_et": float(bar_ts_et.hour),
    }


def hold_exit_tech_from_features(features: Mapping[str, Any]) -> dict[str, float]:
    return {
        "rsi_5m": _safe_float(features.get("rsi_5m")),
        "momentum_2h_pct": _safe_float(features.get("momentum_2h_pct")),
        "volatility_5m_pct": _safe_float(features.get("volatility_5m_pct")),
        "pullback_from_high_pct": _safe_float(features.get("pullback_from_high_pct")),
    }


def context_vector_from_dict(row: Mapping[str, Any], keys: tuple[str, ...]) -> list[float]:
    return [_safe_float(row.get(k)) for k in keys]


def merge_context_into_row(row: dict[str, Any], ctx: Mapping[str, float]) -> None:
    for k, v in ctx.items():
        row[k] = v


__all__ = [
    "ENTRY_CONTEXT_NUMERIC_KEYS",
    "HOLD_BAR_TRAIN_NUMERIC_KEYS",
    "HOLD_ENTRY_SNAPSHOT_KEYS",
    "HOLD_EXIT_CONTEXT_NUMERIC_KEYS",
    "HOLD_EXIT_TECH_KEYS",
    "HOLD_STATE_KEYS",
    "build_entry_context_features",
    "context_vector_from_dict",
    "entry_snapshot_from_context",
    "correlation_matrix_before_date",
    "fetch_calendar_gaps_as_of",
    "fetch_correlation_features_as_of",
    "hold_exit_tech_from_features",
    "prob_direction_from_technical",
    "hold_state_features",
    "infer_kb_news_impact_label",
    "kb_news_impact_enc",
    "kb_news_stats",
    "macro_risk_enc",
    "merge_context_into_row",
    "session_phase_enc_from_ts",
]

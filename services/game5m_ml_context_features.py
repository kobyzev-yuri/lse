"""Decision-time context features for GAME_5M entry/hold ML (news, calendar, macro, state)."""
from __future__ import annotations

import math
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Mapping, Optional

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

_MACRO_RISK_ORDER = ("LOW", "MEDIUM", "HIGH", "EXTREME", "UNKNOWN")


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
) -> dict[str, float]:
    """Premarket/NDX gaps for trade_date from DB; zeros if unavailable."""
    sym = str(ticker or "").strip().upper()
    dkey = str(pd.Timestamp(trade_date).date())
    ck = (sym, dkey)
    if cache is not None and ck in cache:
        return cache[ck]
    out = {
        "ndx_gap_pct": 0.0,
        "spy_gap_pct": 0.0,
        "premarket_gap_pct": 0.0,
        "macro_risk_enc": macro_risk_enc(None),
    }
    if engine is None:
        return out
    try:
        from sqlalchemy import text

        d = pd.Timestamp(trade_date).date()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT premarket_gap_pct, ndx_gap_pct, spy_gap_pct
                    FROM premarket_daily_features
                    WHERE ticker = :ticker AND trade_date = :d
                    LIMIT 1
                    """
                ),
                {"ticker": str(ticker or "").strip().upper(), "d": d},
            ).fetchone()
        if row:
            out["premarket_gap_pct"] = _safe_float(row[0])
            out["ndx_gap_pct"] = _safe_float(row[1])
            out["spy_gap_pct"] = _safe_float(row[2])
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
        engine, ticker=ticker, trade_date=ts.date(), cache=gaps_cache,
    )

    ctx = dict(entry_context or {})
    feat = dict(features or {})

    macro_level = ctx.get("macro_risk_level") or feat.get("macro_risk_level")
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
        "prob_up": _safe_float(ctx.get("prob_up") or feat.get("prob_up")),
        "prob_down": _safe_float(ctx.get("prob_down") or feat.get("prob_down")),
        "llm_sentiment": _safe_float(ctx.get("llm_sentiment") or feat.get("llm_sentiment")),
    }
    for k in CORRELATION_CB_FEATURE_KEYS:
        out[k] = _safe_float(ctx.get(k))
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
    "fetch_calendar_gaps_as_of",
    "hold_exit_tech_from_features",
    "hold_state_features",
    "infer_kb_news_impact_label",
    "kb_news_impact_enc",
    "kb_news_stats",
    "macro_risk_enc",
    "merge_context_into_row",
    "session_phase_enc_from_ts",
]

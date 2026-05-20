"""Payload for web premarket 1m chart (Yahoo prepost, ET axis)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from services.premarket import NYSE_OPEN_TIME, NYSE_TZ, get_premarket_context, get_premarket_ohlc

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    _ET = NYSE_TZ


def _ts_iso(t) -> str:
    if hasattr(t, "isoformat"):
        s = t.isoformat()
        return s if isinstance(s, str) else str(s)
    return str(t)


def _series_to_et(df: pd.DataFrame, dt_col: str) -> pd.Series:
    dts = pd.to_datetime(df[dt_col], errors="coerce")
    if hasattr(dts.dt, "tz") and dts.dt.tz is not None:
        return dts.dt.tz_convert("America/New_York")
    try:
        return dts.dt.tz_localize("America/New_York", ambiguous=True)
    except Exception:
        return dts.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")


def _filter_premarket_bars(df: pd.DataFrame, dt_col: str) -> pd.DataFrame:
    """Сегодня до 9:30 ET; если мало точек — вся сегодняшняя лента 1m."""
    if df is None or df.empty:
        return df
    out = df.copy()
    try:
        et = _series_to_et(out, dt_col)
        today = et.max().date()
        mask_pm = (et.dt.date == today) & (et.dt.time < NYSE_OPEN_TIME)
        sub = out.loc[mask_pm].copy()
        if len(sub) >= 2:
            return sub.reset_index(drop=True)
        mask_today = et.dt.date == today
        sub2 = out.loc[mask_today].copy()
        if len(sub2) >= 2:
            return sub2.reset_index(drop=True)
    except Exception:
        pass
    return out.reset_index(drop=True)


def build_premarket_chart_data(ticker: str) -> Dict[str, Any]:
    """
    Данные для Chart.js: 1m премаркет, линия prev_close, метка открытия 9:30 ET.
    """
    sym = (ticker or "").strip().upper()
    out: Dict[str, Any] = {
        "ticker": sym,
        "times": [],
        "close": [],
        "ohlc": None,
        "prev_close": None,
        "premarket_last": None,
        "premarket_gap_pct": None,
        "premarket_last_time_et": None,
        "minutes_until_open": None,
        "session_phase": None,
        "open_boundary_index": None,
        "no_data": True,
        "error": None,
        "points_count": 0,
    }
    if not sym:
        out["error"] = "пустой тикер"
        return out

    try:
        from services.market_session import get_market_session_context

        sess = get_market_session_context() or {}
        out["session_phase"] = sess.get("session_phase")
    except Exception:
        pass

    pm_ctx = get_premarket_context(sym)
    if pm_ctx.get("error") and not pm_ctx.get("premarket_last"):
        out["error"] = pm_ctx.get("error")
    out["prev_close"] = pm_ctx.get("prev_close")
    out["premarket_last"] = pm_ctx.get("premarket_last")
    out["premarket_gap_pct"] = pm_ctx.get("premarket_gap_pct")
    out["premarket_last_time_et"] = pm_ctx.get("premarket_last_time_et")
    out["minutes_until_open"] = pm_ctx.get("minutes_until_open")

    df = get_premarket_ohlc(sym)
    if df is None or df.empty or "Close" not in df.columns:
        if not out["error"]:
            out["error"] = "нет данных 1m (Yahoo prepost)"
        return out

    dt_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = _filter_premarket_bars(df, dt_col)
    if df.empty:
        out["error"] = "нет баров премаркета на сегодня"
        return out

    try:
        et = _series_to_et(df, dt_col)
        df = df.copy()
        df["_et"] = et
        times = [_ts_iso(t) for t in df["_et"].tolist()]
        close = [float(x) for x in df["Close"].astype(float).tolist()]
        ohlc = None
        if all(c in df.columns for c in ("Open", "High", "Low", "Close")):
            ohlc = {
                "open": [float(x) for x in df["Open"].astype(float).tolist()],
                "high": [float(x) for x in df["High"].astype(float).tolist()],
                "low": [float(x) for x in df["Low"].astype(float).tolist()],
                "close": close,
            }
        open_idx: Optional[int] = None
        for i, ts in enumerate(df["_et"]):
            if ts.time >= NYSE_OPEN_TIME:
                open_idx = i
                break
        out.update({
            "times": times,
            "close": close,
            "ohlc": ohlc,
            "open_boundary_index": open_idx,
            "no_data": False,
            "points_count": len(times),
            "error": None,
        })
    except Exception as e:
        out["error"] = str(e)
    return out


def build_premarket_table_rows(tickers: List[str]) -> List[Dict[str, Any]]:
    """Сводная таблица премаркета (как /premarket в Telegram)."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for raw in tickers or []:
        t = (raw or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        pm = get_premarket_context(t)
        if pm.get("error") and pm.get("premarket_last") is None:
            continue
        rows.append({
            "ticker": t,
            "prev_close": pm.get("prev_close"),
            "premarket_last": pm.get("premarket_last"),
            "premarket_gap_pct": pm.get("premarket_gap_pct"),
            "minutes_until_open": pm.get("minutes_until_open"),
            "premarket_last_time_et": pm.get("premarket_last_time_et"),
        })
    return rows

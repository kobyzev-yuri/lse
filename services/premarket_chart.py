"""Payload for web premarket 1m chart. DB-first; Yahoo only in PRE_MARKET (table)."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from services.premarket import NYSE_OPEN_TIME, NYSE_TZ, get_premarket_context, get_premarket_ohlc

logger = logging.getLogger(__name__)

TABLE_CACHE_TTL_SEC = 120
CHART_CACHE_TTL_SEC = 90
_YAHOO_MAX_WORKERS = 3
_YAHOO_TICKER_TIMEOUT_SEC = 10

_table_cache: Tuple[float, str, List[Dict[str, Any]]] = (0.0, "", [])
_chart_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_cache_lock = Lock()

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    _ET = NYSE_TZ


def _session_phase() -> str:
    try:
        from services.market_session import get_market_session_context

        return str((get_market_session_context() or {}).get("session_phase") or "")
    except Exception:
        return ""


def _yahoo_live_for_table() -> bool:
    """В RTH не опрашиваем Yahoo по всем тикерам — только снимок из БД."""
    return _session_phase() == "PRE_MARKET"


def _ts_iso(t) -> str:
    if hasattr(t, "isoformat"):
        s = t.isoformat()
        return s if isinstance(s, str) else str(s)
    return str(t)


def _et_trade_date() -> date:
    try:
        if _ET is not None:
            return datetime.now(_ET).date()
    except Exception:
        pass
    return datetime.now(timezone.utc).date()


def _series_to_et(df: pd.DataFrame, dt_col: str) -> pd.Series:
    dts = pd.to_datetime(df[dt_col], errors="coerce")
    if hasattr(dts.dt, "tz") and dts.dt.tz is not None:
        return dts.dt.tz_convert("America/New_York")
    try:
        return dts.dt.tz_localize("America/New_York", ambiguous=True)
    except Exception:
        return dts.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")


def _filter_premarket_bars(df: pd.DataFrame, dt_col: str) -> pd.DataFrame:
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
        if len(out) >= 2:
            return out.reset_index(drop=True)
    except Exception:
        pass
    return out.reset_index(drop=True)


def _minutes_until_open_global() -> Optional[int]:
    try:
        from services.premarket import _minutes_until_open, _et_now

        return _minutes_until_open(_et_now())
    except Exception:
        return None


def _rows_from_gap_forecast_db(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    syms = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
    if not syms:
        return {}
    td = _et_trade_date()
    try:
        from sqlalchemy import bindparam, create_engine, text

        from config_loader import get_database_url

        eng = create_engine(get_database_url())
        stmt = text(
            """
            SELECT symbol, prev_close, premarket_last, premarket_gap_pct,
                   snapshot_ts_premarket, open_gap_pct
            FROM game5m_gap_forecast_daily
            WHERE trade_date = :td AND symbol IN :syms
            """
        ).bindparams(bindparam("syms", expanding=True))
        with eng.connect() as conn:
            df = pd.read_sql(stmt, conn, params={"td": td, "syms": syms})
    except Exception as e:
        logger.debug("premarket table DB: %s", e)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    if df is None or df.empty:
        return out
    for _, r in df.iterrows():
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        ts = r.get("snapshot_ts_premarket")
        og = r.get("open_gap_pct")
        out[sym] = {
            "ticker": sym,
            "prev_close": float(r["prev_close"]) if pd.notna(r.get("prev_close")) else None,
            "premarket_last": float(r["premarket_last"]) if pd.notna(r.get("premarket_last")) else None,
            "premarket_gap_pct": float(r["premarket_gap_pct"]) if pd.notna(r.get("premarket_gap_pct")) else None,
            "open_gap_pct": float(og) if pd.notna(og) else None,
            "premarket_last_time_et": str(ts) if ts is not None and pd.notna(ts) else None,
            "source": "db",
        }
    return out


def _yahoo_context_row(ticker: str) -> Optional[Dict[str, Any]]:
    pm = get_premarket_context(ticker)
    if pm.get("error") and pm.get("premarket_last") is None:
        return None
    return {
        "ticker": ticker,
        "prev_close": pm.get("prev_close"),
        "premarket_last": pm.get("premarket_last"),
        "premarket_gap_pct": pm.get("premarket_gap_pct"),
        "minutes_until_open": pm.get("minutes_until_open"),
        "premarket_last_time_et": pm.get("premarket_last_time_et"),
        "source": "yahoo",
    }


def build_premarket_table_rows(
    tickers: List[str],
    *,
    yahoo_fallback: Optional[bool] = None,
    yahoo_max_tickers: int = 8,
) -> List[Dict[str, Any]]:
    if yahoo_fallback is None:
        yahoo_fallback = _yahoo_live_for_table()

    seen: set = set()
    ordered: List[str] = []
    for raw in tickers or []:
        t = (raw or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        ordered.append(t)

    db_map = _rows_from_gap_forecast_db(ordered)
    phase = _session_phase()
    mins_global = _minutes_until_open_global() if phase == "PRE_MARKET" else 0
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []

    for t in ordered:
        if t in db_map and (
            db_map[t].get("premarket_last") is not None or db_map[t].get("premarket_gap_pct") is not None
        ):
            row = dict(db_map[t])
            row["minutes_until_open"] = mins_global
            rows.append(row)
        elif yahoo_fallback:
            missing.append(t)

    if missing and yahoo_fallback:
        todo = missing[: max(0, int(yahoo_max_tickers))]
        with ThreadPoolExecutor(max_workers=_YAHOO_MAX_WORKERS) as pool:
            futs = {pool.submit(_yahoo_context_row, t): t for t in todo}
            try:
                for fut in as_completed(futs, timeout=_YAHOO_TICKER_TIMEOUT_SEC * len(todo) + 5):
                    try:
                        row = fut.result(timeout=_YAHOO_TICKER_TIMEOUT_SEC)
                    except Exception as e:
                        logger.debug("premarket yahoo: %s", e)
                        continue
                    if row:
                        rows.append(row)
            except Exception as e:
                logger.warning("premarket table yahoo batch: %s", e)

    rows.sort(key=lambda r: str(r.get("ticker") or ""))
    return rows


def build_premarket_table_rows_cached(
    tickers: List[str],
    *,
    ttl_sec: int = TABLE_CACHE_TTL_SEC,
) -> Tuple[List[Dict[str, Any]], bool]:
    key = ",".join(sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()}))
    now = time.time()
    with _cache_lock:
        ts, k, rows = _table_cache
        if k == key and (now - ts) < ttl_sec:
            return list(rows), True
    rows = build_premarket_table_rows(tickers)
    with _cache_lock:
        global _table_cache
        _table_cache = (now, key, list(rows))
    return rows, False


def _fill_meta_from_db_or_yahoo(out: Dict[str, Any], sym: str) -> None:
    db_one = _rows_from_gap_forecast_db([sym]).get(sym)
    if db_one:
        out["prev_close"] = db_one.get("prev_close")
        out["premarket_last"] = db_one.get("premarket_last")
        out["premarket_gap_pct"] = db_one.get("premarket_gap_pct")
        out["premarket_last_time_et"] = db_one.get("premarket_last_time_et")
        out["open_gap_pct"] = db_one.get("open_gap_pct")
        out["minutes_until_open"] = _minutes_until_open_global() if _session_phase() == "PRE_MARKET" else 0
        return
    if _yahoo_live_for_table():
        pm_ctx = get_premarket_context(sym)
        if pm_ctx.get("error") and not pm_ctx.get("premarket_last"):
            out["error"] = pm_ctx.get("error")
        out["prev_close"] = pm_ctx.get("prev_close")
        out["premarket_last"] = pm_ctx.get("premarket_last")
        out["premarket_gap_pct"] = pm_ctx.get("premarket_gap_pct")
        out["premarket_last_time_et"] = pm_ctx.get("premarket_last_time_et")
        out["minutes_until_open"] = pm_ctx.get("minutes_until_open")


def build_premarket_chart_data(ticker: str) -> Dict[str, Any]:
    sym = (ticker or "").strip().upper()
    out: Dict[str, Any] = {
        "ticker": sym,
        "times": [],
        "close": [],
        "ohlc": None,
        "prev_close": None,
        "premarket_last": None,
        "premarket_gap_pct": None,
        "open_gap_pct": None,
        "premarket_last_time_et": None,
        "minutes_until_open": None,
        "session_phase": _session_phase(),
        "open_boundary_index": None,
        "no_data": True,
        "error": None,
        "points_count": 0,
        "live_yahoo": _yahoo_live_for_table(),
    }
    if not sym:
        out["error"] = "пустой тикер"
        return out

    _fill_meta_from_db_or_yahoo(out, sym)

    df = get_premarket_ohlc(sym)
    if df is None or df.empty or "Close" not in df.columns:
        if not out["error"]:
            phase = out.get("session_phase") or ""
            if phase in ("REGULAR", "NEAR_OPEN", "NEAR_CLOSE"):
                out["error"] = (
                    "Сейчас идёт RTH — минутки премаркета на графике недоступны. "
                    "Снимок Gap — в таблице (БД premarket_cron); торговля — вкладка 5m."
                )
            else:
                out["error"] = "нет данных 1m (Yahoo prepost)"
        return out

    dt_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = _filter_premarket_bars(df, dt_col)
    if df.empty:
        out["error"] = "нет баров премаркета на сегодня (до 9:30 ET)"
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


def build_premarket_chart_data_cached(
    ticker: str,
    *,
    ttl_sec: int = CHART_CACHE_TTL_SEC,
) -> Tuple[Dict[str, Any], bool]:
    sym = (ticker or "").strip().upper()
    now = time.time()
    with _cache_lock:
        hit = _chart_cache.get(sym)
        if hit and (now - hit[0]) < ttl_sec:
            return dict(hit[1]), True
    data = build_premarket_chart_data(sym)
    with _cache_lock:
        _chart_cache[sym] = (now, dict(data))
    return data, False

"""Payload for web premarket 1m chart (Yahoo prepost, ET axis)."""

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
    """Снимок premarket_cron из game5m_gap_forecast_daily (без Yahoo)."""
    syms = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
    if not syms:
        return {}
    td = _et_trade_date()
    try:
        from sqlalchemy import bindparam, text

        from config_loader import get_database_url
        from sqlalchemy import create_engine

        eng = create_engine(get_database_url())
        stmt = text(
            """
            SELECT symbol, prev_close, premarket_last, premarket_gap_pct,
                   pred_sector_gap_pct, pred_ticker_gap_pct,
                   pred_ticker_source, pred_ticker_model_version,
                   rth_open_price, open_gap_pct, source_open,
                   error_pred_ticker_vs_open_pct,
                   snapshot_ts_premarket
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
        pred_ticker = float(r["pred_ticker_gap_pct"]) if pd.notna(r.get("pred_ticker_gap_pct")) else None
        open_gap = float(r["open_gap_pct"]) if pd.notna(r.get("open_gap_pct")) else None
        pred_err = None
        if pred_ticker is not None and open_gap is not None:
            pred_err = round(open_gap - pred_ticker, 6)
        elif pd.notna(r.get("error_pred_ticker_vs_open_pct")):
            pred_err = float(r["error_pred_ticker_vs_open_pct"])
        out[sym] = {
            "ticker": sym,
            "prev_close": float(r["prev_close"]) if pd.notna(r.get("prev_close")) else None,
            "premarket_last": float(r["premarket_last"]) if pd.notna(r.get("premarket_last")) else None,
            "premarket_gap_pct": float(r["premarket_gap_pct"]) if pd.notna(r.get("premarket_gap_pct")) else None,
            "pred_sector_gap_pct": float(r["pred_sector_gap_pct"]) if pd.notna(r.get("pred_sector_gap_pct")) else None,
            "pred_ticker_gap_pct": pred_ticker,
            "pred_ticker_source": str(r.get("pred_ticker_source") or "") or None,
            "pred_ticker_model_version": str(r.get("pred_ticker_model_version") or "") or None,
            "rth_open_price": float(r["rth_open_price"]) if pd.notna(r.get("rth_open_price")) else None,
            "open_gap_pct": open_gap,
            "source_open": str(r.get("source_open") or "") or None,
            "error_pred_ticker_vs_open_pct": pred_err,
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
        "pred_sector_gap_pct": None,
        "pred_ticker_gap_pct": None,
        "pred_ticker_source": None,
        "pred_ticker_model_version": None,
        "rth_open_price": None,
        "open_gap_pct": None,
        "source_open": None,
        "error_pred_ticker_vs_open_pct": None,
        "minutes_until_open": pm.get("minutes_until_open"),
        "premarket_last_time_et": pm.get("premarket_last_time_et"),
        "source": "yahoo",
    }


def build_premarket_table_rows(
    tickers: List[str],
    *,
    yahoo_fallback: bool = True,
    yahoo_max_tickers: int = 8,
) -> List[Dict[str, Any]]:
    """
    Сводная таблица: сначала БД (premarket_cron), Yahoo только для пропусков (лимит).
    """
    seen: set = set()
    ordered: List[str] = []
    for raw in tickers or []:
        t = (raw or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        ordered.append(t)

    db_map = _rows_from_gap_forecast_db(ordered)
    mins_global = _minutes_until_open_global()
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []

    for t in ordered:
        if t in db_map and db_map[t].get("premarket_last") is not None:
            row = dict(db_map[t])
            row["minutes_until_open"] = mins_global
            rows.append(row)
        elif yahoo_fallback:
            missing.append(t)

    if missing and yahoo_fallback:
        todo = missing[: max(0, int(yahoo_max_tickers))]
        with ThreadPoolExecutor(max_workers=_YAHOO_MAX_WORKERS) as pool:
            futs = {pool.submit(_yahoo_context_row, t): t for t in todo}
            for fut in as_completed(futs, timeout=_YAHOO_TICKER_TIMEOUT_SEC * len(todo) + 5):
                t = futs[fut]
                try:
                    row = fut.result(timeout=_YAHOO_TICKER_TIMEOUT_SEC)
                except Exception as e:
                    logger.debug("premarket yahoo %s: %s", t, e)
                    continue
                if row:
                    rows.append(row)

    rows.sort(key=lambda r: str(r.get("ticker") or ""))
    return rows


def build_premarket_table_rows_cached(
    tickers: List[str],
    *,
    ttl_sec: int = TABLE_CACHE_TTL_SEC,
) -> Tuple[List[Dict[str, Any]], bool]:
    global _table_cache

    key = ",".join(sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()}))
    now = time.time()
    with _cache_lock:
        ts, k, rows = _table_cache
        if k == key and (now - ts) < ttl_sec:
            return list(rows), True
    rows = build_premarket_table_rows(tickers)
    with _cache_lock:
        _table_cache = (now, key, list(rows))
    return rows, False


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
        "pred_sector_gap_pct": None,
        "pred_ticker_gap_pct": None,
        "pred_ticker_source": None,
        "pred_ticker_model_version": None,
        "rth_open_price": None,
        "open_gap_pct": None,
        "source_open": None,
        "error_pred_ticker_vs_open_pct": None,
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

    db_one = _rows_from_gap_forecast_db([sym]).get(sym)
    if db_one:
        out["prev_close"] = db_one.get("prev_close")
        out["premarket_last"] = db_one.get("premarket_last")
        out["premarket_gap_pct"] = db_one.get("premarket_gap_pct")
        out["pred_sector_gap_pct"] = db_one.get("pred_sector_gap_pct")
        out["pred_ticker_gap_pct"] = db_one.get("pred_ticker_gap_pct")
        out["pred_ticker_source"] = db_one.get("pred_ticker_source")
        out["pred_ticker_model_version"] = db_one.get("pred_ticker_model_version")
        out["rth_open_price"] = db_one.get("rth_open_price")
        out["open_gap_pct"] = db_one.get("open_gap_pct")
        out["source_open"] = db_one.get("source_open")
        out["error_pred_ticker_vs_open_pct"] = db_one.get("error_pred_ticker_vs_open_pct")
        out["premarket_last_time_et"] = db_one.get("premarket_last_time_et")
        out["minutes_until_open"] = _minutes_until_open_global()
    else:
        pm_ctx = get_premarket_context(sym)
        if pm_ctx.get("error") and not pm_ctx.get("premarket_last"):
            out["error"] = pm_ctx.get("error")
        out["prev_close"] = pm_ctx.get("prev_close")
        out["premarket_last"] = pm_ctx.get("premarket_last")
        out["premarket_gap_pct"] = pm_ctx.get("premarket_gap_pct")
        out["pred_sector_gap_pct"] = None
        out["pred_ticker_gap_pct"] = None
        out["pred_ticker_source"] = None
        out["pred_ticker_model_version"] = None
        out["rth_open_price"] = None
        out["open_gap_pct"] = None
        out["source_open"] = None
        out["error_pred_ticker_vs_open_pct"] = None
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
            if ts.time() >= NYSE_OPEN_TIME:
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


def invalidate_premarket_cache() -> None:
    global _table_cache

    with _cache_lock:
        _table_cache = (0.0, "", [])
        _chart_cache.clear()

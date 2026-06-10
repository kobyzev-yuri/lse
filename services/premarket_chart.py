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
TABLE_CACHE_TTL_PREMARKET_SEC = 300
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
    except Exception:
        pass
    return out.iloc[0:0].copy().reset_index(drop=True)


def _minutes_until_open_global() -> Optional[int]:
    try:
        from services.premarket import _minutes_until_open, _et_now

        return _minutes_until_open(_et_now())
    except Exception:
        return None


def _snapshot_is_preopen(ts: Any) -> bool:
    if ts is None or pd.isna(ts):
        return True
    try:
        dt = pd.Timestamp(ts)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        else:
            dt = dt.tz_convert("UTC")
        et = dt.tz_convert("America/New_York")
        return et.time() < NYSE_OPEN_TIME
    except Exception:
        return True


def _snapshot_to_et(ts: Any) -> Optional[pd.Timestamp]:
    if ts is None or pd.isna(ts):
        return None
    try:
        dt = pd.Timestamp(ts)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        return dt.tz_convert("America/New_York")
    except Exception:
        return None


def _snapshot_time_et_str(ts: Any) -> Optional[str]:
    et = _snapshot_to_et(ts)
    if et is None:
        return None
    return et.strftime("%Y-%m-%d %H:%M:%S %Z")


def _minutes_until_open_at_snapshot(ts: Any) -> Optional[int]:
    et = _snapshot_to_et(ts)
    if et is None:
        return None
    try:
        open_et = pd.Timestamp.combine(et.date(), NYSE_OPEN_TIME).tz_localize(et.tz)
        return max(0, int((open_et - et).total_seconds() // 60))
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
        snapshot_valid = _snapshot_is_preopen(ts)
        pred_ticker = float(r["pred_ticker_gap_pct"]) if pd.notna(r.get("pred_ticker_gap_pct")) else None
        open_gap = float(r["open_gap_pct"]) if pd.notna(r.get("open_gap_pct")) else None
        pred_err = None
        if pred_ticker is not None and open_gap is not None:
            pred_err = round(open_gap - pred_ticker, 6)
        elif pd.notna(r.get("error_pred_ticker_vs_open_pct")):
            pred_err = float(r["error_pred_ticker_vs_open_pct"])
        snapshot_time_et = _snapshot_time_et_str(ts) if snapshot_valid else None
        out[sym] = {
            "ticker": sym,
            "prev_close": float(r["prev_close"]) if pd.notna(r.get("prev_close")) else None,
            "premarket_last": (
                float(r["premarket_last"]) if snapshot_valid and pd.notna(r.get("premarket_last")) else None
            ),
            "premarket_gap_pct": (
                float(r["premarket_gap_pct"]) if snapshot_valid and pd.notna(r.get("premarket_gap_pct")) else None
            ),
            "pred_sector_gap_pct": (
                float(r["pred_sector_gap_pct"]) if snapshot_valid and pd.notna(r.get("pred_sector_gap_pct")) else None
            ),
            "pred_ticker_gap_pct": pred_ticker,
            "pred_ticker_source": str(r.get("pred_ticker_source") or "") or None,
            "pred_ticker_model_version": str(r.get("pred_ticker_model_version") or "") or None,
            "rth_open_price": float(r["rth_open_price"]) if pd.notna(r.get("rth_open_price")) else None,
            "open_gap_pct": open_gap,
            "source_open": str(r.get("source_open") or "") or None,
            "error_pred_ticker_vs_open_pct": pred_err,
            "minutes_until_open": _minutes_until_open_at_snapshot(ts),
            "premarket_last_time_et": snapshot_time_et,
            "premarket_snapshot_valid": snapshot_valid,
            "source": "db",
            "snapshot_ts_premarket": ts,
            "ml_db_snapshot_ts": _snapshot_time_et_str(ts) if snapshot_valid else None,
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


def _session_phase() -> Optional[str]:
    try:
        from services.market_session import get_market_session_context

        return (get_market_session_context() or {}).get("session_phase")
    except Exception:
        return None


def is_preopen_live() -> bool:
    """До 9:30 ET в торговый день: Yahoo live + пересчёт open-gap прогнозов."""
    phase = (_session_phase() or "").strip().upper()
    if phase in ("WEEKEND", "HOLIDAY"):
        return False
    try:
        from services.premarket import _et_now

        et = _et_now()
        if et is None:
            return phase == "PRE_MARKET"
        return et.time() < NYSE_OPEN_TIME
    except Exception:
        return phase == "PRE_MARKET"


def _macro_risk_cached() -> Optional[Dict[str, Any]]:
    try:
        from services.macro_premarket_risk import evaluate_macro_premarket_risk

        return evaluate_macro_premarket_risk()
    except Exception as e:
        logger.debug("premarket macro risk: %s", e)
        return None


def _apply_live_open_gap_forecasts(
    row: Dict[str, Any],
    *,
    macro_risk: Optional[Dict[str, Any]] = None,
    db_snapshot_ts: Any = None,
    live_preopen: bool = False,
) -> Dict[str, Any]:
    """Recalculate baseline / ML / effective open-gap forecasts from current premarket gap."""
    pm = row.get("premarket_gap_pct")
    if pm is None:
        return row
    out = dict(row)
    if live_preopen:
        # До open не показываем факт/ошибку из БД — только live-прогнозы.
        out["open_gap_pct"] = None
        out["error_pred_ticker_vs_open_pct"] = None
        out["rth_open_price"] = None
        out["source_open"] = None
    try:
        from services.premarket_open_gap_forecast import build_open_gap_forecast_fields

        fc = build_open_gap_forecast_fields(
            str(row.get("ticker") or ""),
            premarket_gap_pct=float(pm),
            macro_risk=macro_risk,
            pred_sector_gap_pct=row.get("pred_sector_gap_pct"),
        )
        out.update(fc)
        if db_snapshot_ts is not None:
            out["ml_db_snapshot_ts"] = db_snapshot_ts
        return out
    except Exception as e:
        logger.debug("live open gap forecast %s: %s", row.get("ticker"), e)
        return out


def build_premarket_table_rows(
    tickers: List[str],
    *,
    yahoo_fallback: bool = True,
    yahoo_max_tickers: int = 8,
    live_premarket: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """
    Сводная таблица: БД (premarket_cron) + в PRE_MARKET live Yahoo и пересчёт open-прогнозов.
    """
    if live_premarket is None:
        live_premarket = is_preopen_live()
    macro = _macro_risk_cached() if live_premarket else None

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

    live_yahoo: Dict[str, Dict[str, Any]] = {}
    if live_premarket and ordered:
        with ThreadPoolExecutor(max_workers=_YAHOO_MAX_WORKERS) as pool:
            futs = {pool.submit(_yahoo_context_row, t): t for t in ordered}
            for fut in as_completed(futs, timeout=_YAHOO_TICKER_TIMEOUT_SEC * len(ordered) + 10):
                t = futs[fut]
                try:
                    yrow = fut.result(timeout=_YAHOO_TICKER_TIMEOUT_SEC)
                except Exception as e:
                    logger.debug("premarket live yahoo %s: %s", t, e)
                    continue
                if yrow:
                    live_yahoo[t] = yrow

    for t in ordered:
        if t in db_map:
            row = dict(db_map[t])
            db_ts = row.get("ml_db_snapshot_ts")
            if live_premarket and t in live_yahoo:
                live = live_yahoo[t]
                row["prev_close"] = live.get("prev_close") if live.get("prev_close") is not None else row.get("prev_close")
                row["premarket_last"] = live.get("premarket_last")
                row["premarket_gap_pct"] = live.get("premarket_gap_pct")
                row["minutes_until_open"] = live.get("minutes_until_open")
                row["premarket_last_time_et"] = live.get("premarket_last_time_et")
                row["premarket_price_source"] = "yahoo_live"
                row["source"] = "db+yahoo_live"
            row = _apply_live_open_gap_forecasts(
                row, macro_risk=macro, db_snapshot_ts=db_ts, live_preopen=live_premarket
            )
            row["minutes_until_open"] = mins_global if mins_global is not None else row.get("minutes_until_open")
            rows.append(row)
        elif yahoo_fallback:
            missing.append(t)

    if missing and yahoo_fallback:
        todo = missing[: max(0, int(yahoo_max_tickers))]
        for t in todo:
            row = live_yahoo.get(t) or _yahoo_context_row(t)
            if not row:
                continue
            row = dict(row)
            row["premarket_price_source"] = "yahoo_live"
            row = _apply_live_open_gap_forecasts(row, macro_risk=macro, live_preopen=live_premarket)
            row["minutes_until_open"] = mins_global if mins_global is not None else row.get("minutes_until_open")
            rows.append(row)

    rows.sort(key=lambda r: str(r.get("ticker") or ""))
    return rows


def build_premarket_table_rows_cached(
    tickers: List[str],
    *,
    ttl_sec: Optional[int] = None,
    skip_cache: bool = False,
) -> Tuple[List[Dict[str, Any]], bool]:
    global _table_cache

    preopen = is_preopen_live()
    if ttl_sec is None:
        ttl_sec = TABLE_CACHE_TTL_PREMARKET_SEC if preopen else TABLE_CACHE_TTL_SEC

    key = ",".join(sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()}))
    key = f"{'preopen' if preopen else 'snap'}|{key}"
    now = time.time()
    if not skip_cache:
        with _cache_lock:
            ts, k, rows = _table_cache
            if k == key and (now - ts) < ttl_sec:
                return list(rows), True
    rows = build_premarket_table_rows(tickers)
    if not skip_cache:
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
    live_preopen = is_preopen_live()
    macro = _macro_risk_cached() if live_preopen else None
    if db_one:
        row = dict(db_one)
        if live_preopen:
            live = _yahoo_context_row(sym)
            if live and live.get("premarket_last") is not None:
                row["prev_close"] = live.get("prev_close") if live.get("prev_close") is not None else row.get("prev_close")
                row["premarket_last"] = live.get("premarket_last")
                row["premarket_gap_pct"] = live.get("premarket_gap_pct")
                row["premarket_last_time_et"] = live.get("premarket_last_time_et")
                row["premarket_price_source"] = "yahoo_live"
        row = _apply_live_open_gap_forecasts(
            row, macro_risk=macro, db_snapshot_ts=row.get("ml_db_snapshot_ts"), live_preopen=live_preopen
        )
        out["prev_close"] = row.get("prev_close")
        out["premarket_last"] = row.get("premarket_last")
        out["premarket_gap_pct"] = row.get("premarket_gap_pct")
        out["pred_sector_gap_pct"] = row.get("pred_sector_gap_pct")
        out["pred_ticker_gap_pct"] = row.get("pred_ticker_gap_pct")
        out["ml_open_gap_pct"] = row.get("ml_open_gap_pct")
        out["baseline_open_gap_pct"] = row.get("baseline_open_gap_pct")
        out["effective_open_gap_pct"] = row.get("effective_open_gap_pct")
        out["effective_open_gap_source"] = row.get("effective_open_gap_source")
        out["pred_ticker_source"] = row.get("pred_ticker_source")
        out["pred_ticker_model_version"] = row.get("pred_ticker_model_version")
        out["rth_open_price"] = row.get("rth_open_price")
        out["open_gap_pct"] = row.get("open_gap_pct")
        out["source_open"] = row.get("source_open")
        out["error_pred_ticker_vs_open_pct"] = row.get("error_pred_ticker_vs_open_pct")
        out["premarket_last_time_et"] = row.get("premarket_last_time_et")
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

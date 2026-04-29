#!/usr/bin/env python3
"""
Collect compact premarket features for ML.

Run on VM:
  docker compose exec lse python scripts/ingest_premarket_daily_features.py --ensure-table

Typical cron: run during PRE_MARKET a few times before 09:30 ET.
The script does not place trades; it only upserts rows into premarket_daily_features.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import Engine, create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url
from services.market_session import get_market_session_context
from services.ticker_groups import (
    get_tickers_for_5m_correlation,
    get_tickers_for_portfolio_game,
    get_tickers_game_5m,
    get_tickers_indicator_only,
)

try:
    from zoneinfo import ZoneInfo

    NYSE_TZ = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover - Python 3.11 has zoneinfo
    NYSE_TZ = None


logger = logging.getLogger(__name__)

NYSE_PREMARKET_START = time(4, 0)
NYSE_OPEN = time(9, 30)

UPSERT_SQL = """
INSERT INTO premarket_daily_features (
  exchange, symbol, trade_date, snapshot_label, snapshot_ts_utc, snapshot_time_et,
  minutes_until_open, prev_close, daily_volatility_5,
  premarket_open, premarket_high, premarket_low, premarket_last, premarket_vwap,
  premarket_volume, premarket_bar_count, premarket_gap_pct, premarket_return_pct,
  premarket_range_pct, gap_vs_daily_volatility, source, updated_at
)
VALUES (
  :exchange, :symbol, :trade_date, :snapshot_label, :snapshot_ts_utc, :snapshot_time_et,
  :minutes_until_open, :prev_close, :daily_volatility_5,
  :premarket_open, :premarket_high, :premarket_low, :premarket_last, :premarket_vwap,
  :premarket_volume, :premarket_bar_count, :premarket_gap_pct, :premarket_return_pct,
  :premarket_range_pct, :gap_vs_daily_volatility, :source, NOW()
)
ON CONFLICT (exchange, symbol, trade_date, snapshot_label) DO UPDATE SET
  snapshot_ts_utc = EXCLUDED.snapshot_ts_utc,
  snapshot_time_et = EXCLUDED.snapshot_time_et,
  minutes_until_open = EXCLUDED.minutes_until_open,
  prev_close = EXCLUDED.prev_close,
  daily_volatility_5 = EXCLUDED.daily_volatility_5,
  premarket_open = EXCLUDED.premarket_open,
  premarket_high = EXCLUDED.premarket_high,
  premarket_low = EXCLUDED.premarket_low,
  premarket_last = EXCLUDED.premarket_last,
  premarket_vwap = EXCLUDED.premarket_vwap,
  premarket_volume = EXCLUDED.premarket_volume,
  premarket_bar_count = EXCLUDED.premarket_bar_count,
  premarket_gap_pct = EXCLUDED.premarket_gap_pct,
  premarket_return_pct = EXCLUDED.premarket_return_pct,
  premarket_range_pct = EXCLUDED.premarket_range_pct,
  gap_vs_daily_volatility = EXCLUDED.gap_vs_daily_volatility,
  source = EXCLUDED.source,
  updated_at = NOW()
"""


def _apply_schema(engine: Engine) -> None:
    sql_path = project_root / "db" / "knowledge_pg" / "sql" / "022_premarket_daily_features.sql"
    ddl = sql_path.read_text(encoding="utf-8")
    parts = [p.strip() for p in ddl.split(";")]
    with engine.begin() as conn:
        for part in parts:
            if part:
                conn.execute(text(part + ";"))


def _dedupe(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        t = (item or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _default_tickers(include_indicators: bool) -> List[str]:
    tickers = _dedupe(
        list(get_tickers_game_5m() or [])
        + list(get_tickers_for_portfolio_game() or [])
        + list(get_tickers_for_5m_correlation() or [])
    )
    if include_indicators:
        return tickers
    indicator_only = {t.upper() for t in (get_tickers_indicator_only() or [])}
    return [t for t in tickers if not t.startswith("^") and t not in indicator_only]


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _pct(after: Optional[float], before: Optional[float]) -> Optional[float]:
    if after is None or before is None or before <= 0:
        return None
    return (after / before - 1.0) * 100.0


def _get_prev_close_and_volatility(engine: Engine, ticker: str, trade_date: datetime.date) -> tuple[Optional[float], Optional[float]]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT close, volatility_5
                FROM quotes
                WHERE ticker = :ticker
                  AND date::date < :trade_date
                ORDER BY date DESC
                LIMIT 1
                """
            ),
            {"ticker": ticker, "trade_date": trade_date},
        ).fetchone()
    if not row:
        return None, None
    return _to_float(row[0]), _to_float(row[1])


def _fetch_premarket_1m(ticker: str) -> Optional[pd.DataFrame]:
    import yfinance as yf

    df = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True, auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.rename_axis("Datetime").reset_index()
    if "Datetime" not in df.columns and "Date" in df.columns:
        df = df.rename(columns={"Date": "Datetime"})
    required = {"Datetime", "Open", "High", "Low", "Close"}
    if not required.issubset(set(df.columns)):
        return None
    dts = pd.to_datetime(df["Datetime"])
    if dts.dt.tz is None:
        dts = dts.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        dts = dts.dt.tz_convert("America/New_York")
    df = df.copy()
    df["Datetime"] = dts
    return df.sort_values("Datetime").reset_index(drop=True)


def _premarket_slice(df: pd.DataFrame, et_now: datetime) -> pd.DataFrame:
    dts = pd.to_datetime(df["Datetime"])
    trade_date = et_now.date()
    mask = (
        (dts.dt.date == trade_date)
        & (dts.dt.time >= NYSE_PREMARKET_START)
        & (dts.dt.time < NYSE_OPEN)
        & (dts <= et_now)
    )
    return df.loc[mask].copy().sort_values("Datetime").reset_index(drop=True)


def _vwap(df: pd.DataFrame) -> Optional[float]:
    if "Volume" not in df.columns:
        return None
    vol = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0)
    close = pd.to_numeric(df["Close"], errors="coerce")
    total_vol = float(vol.sum())
    if total_vol <= 0:
        return None
    return float((close * vol).sum() / total_vol)


def build_feature_row(
    engine: Engine,
    ticker: str,
    *,
    exchange: str,
    snapshot_label: str,
    et_now: datetime,
) -> Optional[Dict[str, Any]]:
    df = _fetch_premarket_1m(ticker)
    if df is None or df.empty:
        logger.warning("%s: no Yahoo premarket 1m data", ticker)
        return None
    pm = _premarket_slice(df, et_now)
    if pm.empty:
        logger.warning("%s: no rows in ET premarket window", ticker)
        return None

    prev_close, daily_volatility = _get_prev_close_and_volatility(engine, ticker, et_now.date())

    p_open = _to_float(pm["Open"].iloc[0])
    p_high = _to_float(pd.to_numeric(pm["High"], errors="coerce").max())
    p_low = _to_float(pd.to_numeric(pm["Low"], errors="coerce").min())
    p_last = _to_float(pm["Close"].iloc[-1])
    p_vwap = _vwap(pm)
    p_volume = None
    if "Volume" in pm.columns:
        vol_sum = pd.to_numeric(pm["Volume"], errors="coerce").fillna(0).sum()
        if not pd.isna(vol_sum):
            p_volume = int(vol_sum)

    gap_pct = _pct(p_last, prev_close)
    return_pct = _pct(p_last, p_open)
    range_pct = _pct(p_high, p_low)
    gap_vs_vol = None
    if gap_pct is not None and daily_volatility is not None and daily_volatility > 0:
        # volatility_5 is stored as a price stddev in quotes, so convert it to percent of prev close.
        vol_pct = _pct((prev_close or 0.0) + daily_volatility, prev_close)
        if vol_pct is not None and vol_pct > 0:
            gap_vs_vol = gap_pct / vol_pct

    open_et = datetime.combine(et_now.date(), NYSE_OPEN, tzinfo=NYSE_TZ)
    minutes_until_open = max(0, int((open_et - et_now).total_seconds() // 60))
    snapshot_utc = et_now.astimezone(timezone.utc)

    return {
        "exchange": exchange,
        "symbol": ticker.upper(),
        "trade_date": et_now.date(),
        "snapshot_label": snapshot_label,
        "snapshot_ts_utc": snapshot_utc,
        "snapshot_time_et": et_now.replace(tzinfo=None),
        "minutes_until_open": minutes_until_open,
        "prev_close": prev_close,
        "daily_volatility_5": daily_volatility,
        "premarket_open": p_open,
        "premarket_high": p_high,
        "premarket_low": p_low,
        "premarket_last": p_last,
        "premarket_vwap": p_vwap,
        "premarket_volume": p_volume,
        "premarket_bar_count": int(len(pm)),
        "premarket_gap_pct": gap_pct,
        "premarket_return_pct": return_pct,
        "premarket_range_pct": range_pct,
        "gap_vs_daily_volatility": gap_vs_vol,
        "source": "yfinance_1m_prepost",
    }


def _auto_snapshot_label(et_now: datetime) -> str:
    return et_now.strftime("%H%M_ET")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect premarket_daily_features from Yahoo 1m prepost data.")
    parser.add_argument("--tickers", default="", help="Comma-separated symbols; default = GAME_5M + portfolio + 5m correlation universe.")
    parser.add_argument("--exchange", default="US", help="Exchange code stored in table PK.")
    parser.add_argument("--snapshot-label", default="auto", help="Label in PK; 'auto' = HHMM_ET, 'latest' overwrites one row per day.")
    parser.add_argument("--include-indicators", action="store_true", help="Include ^VIX and configured indicator-only tickers.")
    parser.add_argument("--force", action="store_true", help="Run outside PRE_MARKET; useful for manual backfill of today's premarket.")
    parser.add_argument("--ensure-table", action="store_true", help="Apply DDL before ingest.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if NYSE_TZ is None:
        raise SystemExit("zoneinfo America/New_York is unavailable")

    session = get_market_session_context()
    phase = (session.get("session_phase") or "").strip()
    if phase != "PRE_MARKET" and not args.force:
        logger.info("Not PRE_MARKET (phase=%s, ET=%s); use --force for manual run.", phase, session.get("et_now"))
        return

    et_now = datetime.now(NYSE_TZ)
    label = _auto_snapshot_label(et_now) if args.snapshot_label == "auto" else args.snapshot_label.strip()
    if not label:
        raise SystemExit("--snapshot-label must not be empty")

    if args.tickers.strip():
        tickers = _dedupe(args.tickers.split(","))
    else:
        tickers = _default_tickers(include_indicators=args.include_indicators)
    if not tickers:
        raise SystemExit("No tickers to ingest")

    engine = create_engine(get_database_url())
    if args.ensure_table:
        _apply_schema(engine)

    rows: List[Dict[str, Any]] = []
    for ticker in tickers:
        try:
            row = build_feature_row(engine, ticker, exchange=args.exchange, snapshot_label=label, et_now=et_now)
            if row is not None:
                rows.append(row)
                logger.info(
                    "%s: gap=%s%% ret=%s%% bars=%s label=%s",
                    ticker,
                    None if row["premarket_gap_pct"] is None else round(float(row["premarket_gap_pct"]), 3),
                    None if row["premarket_return_pct"] is None else round(float(row["premarket_return_pct"]), 3),
                    row["premarket_bar_count"],
                    label,
                )
        except Exception as e:
            logger.warning("%s: %s", ticker, e)

    if not rows:
        logger.warning("No premarket feature rows built")
        return

    with engine.begin() as conn:
        for row in rows:
            clean = {k: (None if isinstance(v, float) and (np.isnan(v) or np.isinf(v)) else v) for k, v in row.items()}
            conn.execute(text(UPSERT_SQL), clean)
    logger.info("Upserted %d rows into premarket_daily_features", len(rows))


if __name__ == "__main__":
    main()

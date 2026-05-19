#!/usr/bin/env python3
"""
UPSERT daily market regime snapshot (SPY, QQQ as NDX proxy, DIA, ^VIX) into market_regime_daily.

Reads daily closes from public.quotes. Log-returns use natural log (project convention).

  python scripts/ingest_market_regime_daily.py --days 400
  python scripts/ingest_market_regime_daily.py --from-date 2024-01-01 --dry-run
  python scripts/ingest_market_regime_daily.py --ensure-table
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url
from services.ingest_multiday_lr_daily_features_common import (
    default_date_range,
    parse_date_arg,
    trading_dates_from_quotes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# quotes ticker -> market_regime_daily column
INDEX_MAP: Dict[str, str] = {
    "SPY": "spy_close",
    "QQQ": "ndx_close",
    "DIA": "dia_close",
    "^VIX": "vix_close",
}

UPSERT_SQL = """
INSERT INTO market_regime_daily (
  trade_date, spy_close, ndx_close, dia_close, vix_close,
  regime_flags, features_json, updated_at
)
VALUES (
  :trade_date, :spy_close, :ndx_close, :dia_close, :vix_close,
  CAST(:regime_flags AS jsonb), CAST(:features_json AS jsonb), NOW()
)
ON CONFLICT (trade_date) DO UPDATE SET
  spy_close = EXCLUDED.spy_close,
  ndx_close = EXCLUDED.ndx_close,
  dia_close = EXCLUDED.dia_close,
  vix_close = EXCLUDED.vix_close,
  regime_flags = EXCLUDED.regime_flags,
  features_json = EXCLUDED.features_json,
  updated_at = NOW()
"""


def _vix_regime(vix: Optional[float]) -> str:
    if vix is None:
        return "NO_DATA"
    if vix >= 25:
        return "HIGH_PANIC"
    if vix <= 15:
        return "LOW_FEAR"
    return "NEUTRAL"


def _log_return(cur: float, prev: float) -> Optional[float]:
    if prev is None or cur is None or prev <= 0 or cur <= 0:
        return None
    return math.log(cur / prev)


def _ensure_table(engine) -> None:
    """Idempotent DDL from scripts/sql/ml_event_analytics_schema.sql (all analytics tables)."""
    sql_path = project_root / "scripts" / "sql" / "ml_event_analytics_schema.sql"
    raw = sql_path.read_text(encoding="utf-8")
    buf: List[str] = []
    for line in raw.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.strip().endswith(";"):
            stmt = "\n".join(buf).strip()
            buf = []
            if stmt:
                with engine.begin() as conn:
                    conn.execute(text(stmt))


def _calendar_trade_dates(engine, d0: date, d1: date) -> Tuple[List[date], str]:
    """Prefer SPY calendar; fall back to any index series present in quotes."""
    for sym in ("SPY", "QQQ", "DIA", "^VIX"):
        dates = trading_dates_from_quotes(engine, sym, d0, d1)
        if dates:
            return dates, sym
    return [], ""


def _missing_index_tickers(engine, d0: date, d1: date) -> List[str]:
    out: List[str] = []
    for sym in INDEX_MAP:
        if not trading_dates_from_quotes(engine, sym, d0, d1):
            out.append(sym)
    return out


def _seed_index_quotes(tickers: List[str], days_back: int, force_days: int) -> None:
    from update_prices import update_all_prices

    logger.info("Seeding quotes (yfinance): %s, force_days_back=%s", ", ".join(tickers), force_days)
    update_all_prices(tickers=tickers, days_back=max(days_back, 120), force_days_back=force_days)


def _load_closes(engine, tickers: List[str], d0: date, d1: date) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                """
                SELECT ticker, date::date AS trade_date, close::float AS close
                FROM public.quotes
                WHERE ticker = ANY(:tickers)
                  AND date::date >= :d0 AND date::date <= :d1
                ORDER BY ticker, date
                """
            ),
            conn,
            params={"tickers": tickers, "d0": d0, "d1": d1},
        )
    if df is None or df.empty:
        return pd.DataFrame(columns=["ticker", "trade_date", "close"])
    df["ticker"] = df["ticker"].astype(str).str.strip().upper()
    return df


def _build_rows(
    closes: pd.DataFrame,
    trade_dates: List[date],
) -> List[Dict[str, Any]]:
    if not trade_dates:
        return []
    piv: Dict[str, pd.Series] = {}
    for tkr, col in INDEX_MAP.items():
        sub = closes[closes["ticker"] == tkr]
        if sub.empty:
            piv[col] = pd.Series(dtype=float)
        else:
            s = sub.set_index("trade_date")["close"].sort_index()
            piv[col] = s
    rows: List[Dict[str, Any]] = []
    prev: Dict[str, Optional[float]] = {c: None for c in INDEX_MAP.values()}
    for td in sorted(trade_dates):
        cur = {col: (float(piv[col][td]) if col in piv and td in piv[col].index else None) for col in INDEX_MAP.values()}
        feats: Dict[str, Any] = {"builder_version": "quotes_regime_v1", "ndx_ticker": "QQQ"}
        flags: Dict[str, Any] = {}
        for tkr, col in INDEX_MAP.items():
            lr = _log_return(cur[col], prev[col]) if cur[col] is not None else None
            if lr is not None:
                feats[f"log_ret_1d_{col.replace('_close', '')}"] = round(lr, 8)
            prev[col] = cur[col]
        vix = cur.get("vix_close")
        flags["vix_regime"] = _vix_regime(vix)
        spy_lr = feats.get("log_ret_1d_spy")
        if spy_lr is not None and spy_lr <= math.log(0.99):
            flags["spy_stress_1d"] = True
        rows.append(
            {
                "trade_date": td,
                "spy_close": cur["spy_close"],
                "ndx_close": cur["ndx_close"],
                "dia_close": cur["dia_close"],
                "vix_close": cur["vix_close"],
                "regime_flags": json.dumps(flags, ensure_ascii=False),
                "features_json": json.dumps(feats, ensure_ascii=False),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest market_regime_daily from quotes")
    parser.add_argument("--days", type=int, default=400, help="Lookback when from/to omitted")
    parser.add_argument("--from-date", type=str, default="")
    parser.add_argument("--to-date", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ensure-table", action="store_true")
    parser.add_argument(
        "--no-auto-seed",
        action="store_true",
        help="Do not fetch missing SPY/QQQ/DIA/^VIX into quotes via yfinance",
    )
    args = parser.parse_args()

    if args.from_date and args.to_date:
        d0, d1 = parse_date_arg(args.from_date), parse_date_arg(args.to_date)
    elif args.from_date:
        d0 = parse_date_arg(args.from_date)
        d1 = default_date_range(args.days)[1]
    else:
        d0, d1 = default_date_range(args.days)

    engine = create_engine(get_database_url())
    if args.ensure_table:
        _ensure_table(engine)

    tickers = list(INDEX_MAP.keys())
    if not args.no_auto_seed and not args.dry_run:
        missing = _missing_index_tickers(engine, d0, d1)
        if missing:
            force = max(30, (d1 - d0).days + 30)
            _seed_index_quotes(missing, days_back=args.days, force_days=force)

    trade_dates, cal_sym = _calendar_trade_dates(engine, d0, d1)
    if not trade_dates:
        logger.error("No index quotes (SPY/QQQ/DIA/^VIX) between %s and %s", d0, d1)
        return 1
    logger.info("Calendar from %s: %s trading days", cal_sym, len(trade_dates))
    closes = _load_closes(engine, tickers, d0, d1)
    missing = [t for t in tickers if t not in set(closes["ticker"].unique())] if not closes.empty else tickers
    if missing:
        logger.warning("Missing quote series: %s", ", ".join(missing))

    rows = _build_rows(closes, trade_dates)
    logger.info("Prepared %s rows (%s .. %s)", len(rows), trade_dates[0], trade_dates[-1])

    if args.dry_run:
        for sample in rows[-3:]:
            logger.info("sample %s vix=%s flags=%s", sample["trade_date"], sample["vix_close"], sample["regime_flags"])
        return 0

    n = 0
    with engine.begin() as conn:
        for row in rows:
            conn.execute(text(UPSERT_SQL), row)
            n += 1
    logger.info("Upserted %s rows into market_regime_daily", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

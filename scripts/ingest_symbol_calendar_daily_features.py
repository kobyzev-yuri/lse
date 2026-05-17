#!/usr/bin/env python3
"""
Backfill symbol_calendar_daily_features (earnings proximity) from knowledge_base.

  python scripts/ingest_symbol_calendar_daily_features.py --tickers-source game5m --from-date 2024-01-01 --ensure-table
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url
from services.ingest_multiday_lr_daily_features_common import (
    apply_sql_migrations,
    default_date_range,
    parse_date_arg,
    resolve_tickers_for_ingest,
    session_close_utc,
    trading_dates_from_quotes,
    trading_day_offset,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO symbol_calendar_daily_features (
  exchange, symbol, trade_date, snapshot_label, snapshot_ts_utc,
  days_to_next_earnings, days_since_last_earnings,
  is_earnings_day, earnings_within_3d, next_earnings_importance,
  events_used, source, updated_at
)
VALUES (
  :exchange, :symbol, :trade_date, :snapshot_label, NOW(),
  :days_to_next_earnings, :days_since_last_earnings,
  :is_earnings_day, :earnings_within_3d, :next_earnings_importance,
  :events_used, :source, NOW()
)
ON CONFLICT (exchange, symbol, trade_date, snapshot_label) DO UPDATE SET
  snapshot_ts_utc = NOW(),
  days_to_next_earnings = EXCLUDED.days_to_next_earnings,
  days_since_last_earnings = EXCLUDED.days_since_last_earnings,
  is_earnings_day = EXCLUDED.is_earnings_day,
  earnings_within_3d = EXCLUDED.earnings_within_3d,
  next_earnings_importance = EXCLUDED.next_earnings_importance,
  events_used = EXCLUDED.events_used,
  source = EXCLUDED.source,
  updated_at = NOW()
"""


def _load_earnings_events(engine, symbol: str, d0: date, d1: date) -> List[Tuple[datetime, Optional[str]]]:
    sym = str(symbol).strip().upper()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT ts, importance
                FROM knowledge_base
                WHERE UPPER(TRIM(ticker)) = :sym
                  AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
                  AND ts IS NOT NULL
                ORDER BY ts ASC
                """
            ),
            {"sym": sym},
        ).fetchall()
    out: List[Tuple[datetime, Optional[str]]] = []
    for ts, imp in rows:
        if ts is None:
            continue
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        out.append((t.to_pydatetime().replace(tzinfo=None), str(imp).strip().upper() if imp else None))
    return out


def _event_trade_date(event_ts_utc: datetime) -> date:
    return pd.Timestamp(event_ts_utc, tz="UTC").tz_convert("America/New_York").date()


def _features_for_day(
    trade_d: date,
    tdates: List[date],
    earnings: List[Tuple[datetime, Optional[str]]],
) -> Dict[str, Any]:
    asof = session_close_utc(trade_d)
    asof_ts = pd.Timestamp(asof, tz="UTC")
    next_ev: Optional[Tuple[datetime, Optional[str]]] = None
    prev_ev: Optional[Tuple[datetime, Optional[str]]] = None
    for ets, imp in earnings:
        ets_ts = pd.Timestamp(ets, tz="UTC")
        if ets_ts > asof_ts:
            next_ev = (ets, imp)
            break
        prev_ev = (ets, imp)

    days_to = days_since = None
    is_day = 0
    within_3d = 0
    next_imp: Optional[str] = None
    if next_ev:
        next_d = _event_trade_date(next_ev[0])
        days_to = trading_day_offset(tdates, trade_d, next_d)
        if days_to is None and next_d >= trade_d:
            days_to = int((pd.Timestamp(next_d) - pd.Timestamp(trade_d)).days)
        next_imp = next_ev[1]
        if days_to is not None and 0 <= days_to <= 3:
            within_3d = 1
    if prev_ev:
        prev_d = _event_trade_date(prev_ev[0])
        days_since = trading_day_offset(tdates, prev_d, trade_d)
        if days_since is None and trade_d >= prev_d:
            days_since = int((pd.Timestamp(trade_d) - pd.Timestamp(prev_d)).days)

    for ets, _ in earnings:
        if _event_trade_date(ets) == trade_d:
            is_day = 1
            break

    return {
        "days_to_next_earnings": days_to,
        "days_since_last_earnings": days_since,
        "is_earnings_day": is_day,
        "earnings_within_3d": within_3d,
        "next_earnings_importance": next_imp,
        "events_used": len(earnings),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest symbol_calendar_daily_features from KB earnings.")
    p.add_argument("--tickers-source", choices=("game5m", "config", "manual"), default="game5m")
    p.add_argument("tickers", nargs="*")
    p.add_argument("--from-date", type=str, default="")
    p.add_argument("--to-date", type=str, default="")
    p.add_argument("--period-days", type=int, default=400)
    p.add_argument("--exchange", default="US")
    p.add_argument("--snapshot-label", default="latest")
    p.add_argument("--ensure-table", action="store_true")
    args = p.parse_args()

    engine = create_engine(get_database_url())
    if args.ensure_table:
        apply_sql_migrations(engine, ("025_symbol_calendar_daily_features.sql",))

    if args.tickers_source == "manual":
        symbols = [str(t).strip().upper() for t in args.tickers if str(t).strip()]
    else:
        symbols = resolve_tickers_for_ingest(args.tickers_source, engine)
    if not symbols:
        logger.error("No tickers")
        return 1

    if args.from_date:
        d0 = parse_date_arg(args.from_date)
        d1 = parse_date_arg(args.to_date) if args.to_date else default_date_range(1)[1]
    else:
        d0, d1 = default_date_range(args.period_days)

    total = 0
    for sym in symbols:
        tdates = trading_dates_from_quotes(engine, sym, d0, d1)
        if not tdates:
            logger.warning("%s: no quotes dates", sym)
            continue
        earnings = _load_earnings_events(engine, sym, d0, d1)
        rows: List[Dict[str, Any]] = []
        for td in tdates:
            feat = _features_for_day(td, tdates, earnings)
            rows.append(
                {
                    "exchange": args.exchange,
                    "symbol": sym,
                    "trade_date": td,
                    "snapshot_label": args.snapshot_label,
                    "source": "knowledge_base_earnings",
                    **feat,
                }
            )
        with engine.begin() as conn:
            for row in rows:
                conn.execute(text(UPSERT_SQL), row)
        total += len(rows)
        earn_days = sum(1 for r in rows if int(r.get("is_earnings_day") or 0))
        logger.info("%s: upserted %d days (%d earnings days, %d KB events)", sym, len(rows), earn_days, len(earnings))

    logger.info("Done: %d rows for %d symbols", total, len(symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

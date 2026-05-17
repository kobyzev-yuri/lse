#!/usr/bin/env python3
"""
Backfill macro_calendar_daily_features from knowledge_base (Investing economic calendar).

  python scripts/ingest_macro_calendar_daily_features.py --from-date 2024-01-01 --ensure-table
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO macro_calendar_daily_features (
  exchange, region, trade_date, snapshot_label, snapshot_ts_utc,
  high_impact_fwd_1d, high_impact_fwd_3d, high_impact_back_1d,
  hours_to_next_high_impact, hours_since_last_high_impact,
  calendar_events_used, source, updated_at
)
VALUES (
  :exchange, :region, :trade_date, :snapshot_label, NOW(),
  :high_impact_fwd_1d, :high_impact_fwd_3d, :high_impact_back_1d,
  :hours_to_next_high_impact, :hours_since_last_high_impact,
  :calendar_events_used, :source, NOW()
)
ON CONFLICT (exchange, region, trade_date, snapshot_label) DO UPDATE SET
  snapshot_ts_utc = NOW(),
  high_impact_fwd_1d = EXCLUDED.high_impact_fwd_1d,
  high_impact_fwd_3d = EXCLUDED.high_impact_fwd_3d,
  high_impact_back_1d = EXCLUDED.high_impact_back_1d,
  hours_to_next_high_impact = EXCLUDED.hours_to_next_high_impact,
  hours_since_last_high_impact = EXCLUDED.hours_since_last_high_impact,
  calendar_events_used = EXCLUDED.calendar_events_used,
  source = EXCLUDED.source,
  updated_at = NOW()
"""


def _load_calendar_events(engine, d0: date, d1: date, region_filter: str) -> pd.DataFrame:
    pad0 = datetime.combine(d0, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=3)
    pad1 = datetime.combine(d1, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=5)
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                """
                SELECT ts, importance, event_type, content, region
                FROM knowledge_base
                WHERE ticker IN ('MACRO', 'US_MACRO')
                  AND source ILIKE '%Investing.com%Economic%Calendar%'
                  AND ts >= :t0 AND ts <= :t1
                """
            ),
            conn,
            params={"t0": pad0.replace(tzinfo=None), "t1": pad1.replace(tzinfo=None)},
        )
    if df is None or df.empty:
        return pd.DataFrame()
    df["_ts"] = pd.to_datetime(df["ts"], utc=True)
    if region_filter.upper() == "US":
        df = df[df["region"].astype(str).str.upper().isin(("USA", "US", "UNITED STATES", "")) | df["region"].isna()]
    imp = df["importance"].astype(str).str.strip().str.upper()
    df["_high"] = imp == "HIGH"
    return df


def _features_for_day(events: pd.DataFrame, trade_d: date) -> Dict[str, Any]:
    asof = pd.Timestamp(session_close_utc(trade_d), tz="UTC")
    if events.empty:
        return {
            "high_impact_fwd_1d": 0,
            "high_impact_fwd_3d": 0,
            "high_impact_back_1d": 0,
            "hours_to_next_high_impact": None,
            "hours_since_last_high_impact": None,
            "calendar_events_used": 0,
        }
    ts = events["_ts"]
    high = events["_high"]
    t1 = asof + pd.Timedelta(hours=24)
    t3 = asof + pd.Timedelta(hours=72)
    tb = asof - pd.Timedelta(hours=24)
    hi = events[high]
    fwd1 = int(((ts > asof) & (ts <= t1) & high).sum())
    fwd3 = int(((ts > asof) & (ts <= t3) & high).sum())
    back1 = int(((ts > tb) & (ts <= asof) & high).sum())
    future_hi = hi[(hi["_ts"] > asof)]
    past_hi = hi[(hi["_ts"] <= asof)]
    h_next: Optional[float] = None
    h_since: Optional[float] = None
    if not future_hi.empty:
        dt_h = (future_hi["_ts"].min() - asof).total_seconds() / 3600.0
        h_next = float(min(max(dt_h, 0.0), 168.0))
    if not past_hi.empty:
        dt_s = (asof - past_hi["_ts"].max()).total_seconds() / 3600.0
        h_since = float(min(max(dt_s, 0.0), 168.0))
    return {
        "high_impact_fwd_1d": fwd1,
        "high_impact_fwd_3d": fwd3,
        "high_impact_back_1d": back1,
        "hours_to_next_high_impact": h_next,
        "hours_since_last_high_impact": h_since,
        "calendar_events_used": int(len(events)),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest macro_calendar_daily_features from KB.")
    p.add_argument("--from-date", type=str, default="")
    p.add_argument("--to-date", type=str, default="")
    p.add_argument("--period-days", type=int, default=400)
    p.add_argument("--region", default="US")
    p.add_argument("--exchange", default="US")
    p.add_argument("--snapshot-label", default="latest")
    p.add_argument(
        "--anchor-symbol",
        default="",
        help="quotes trading calendar anchor (default: first GAME_5M ticker with quotes, else MU)",
    )
    p.add_argument("--ensure-table", action="store_true")
    args = p.parse_args()

    engine = create_engine(get_database_url())
    if args.ensure_table:
        apply_sql_migrations(engine, ("024_macro_calendar_daily_features.sql",))

    if args.from_date:
        d0 = parse_date_arg(args.from_date)
        d1 = parse_date_arg(args.to_date) if args.to_date else default_date_range(1)[1]
    else:
        d0, d1 = default_date_range(args.period_days)

    anchor = (args.anchor_symbol or "").strip().upper()
    tdates: List[date] = []
    if anchor:
        tdates = trading_dates_from_quotes(engine, anchor, d0, d1)
    if not tdates:
        for sym in resolve_tickers_for_ingest("game5m", engine) + ["MU", "SNDK", "ASML"]:
            tdates = trading_dates_from_quotes(engine, sym, d0, d1)
            if tdates:
                anchor = sym
                logger.info("Using quotes anchor symbol %s (%d trading days)", anchor, len(tdates))
                break
    if not tdates:
        logger.error("No trading dates from quotes (tried GAME_5M tickers and MU/SNDK/ASML)")
        return 1

    events = _load_calendar_events(engine, d0, d1, args.region)
    logger.info("Loaded %d calendar KB rows (padded window)", len(events))

    rows: List[Dict[str, Any]] = []
    for td in tdates:
        feat = _features_for_day(events, td)
        rows.append(
            {
                "exchange": args.exchange,
                "region": args.region,
                "trade_date": td,
                "snapshot_label": args.snapshot_label,
                "source": "investing_calendar_kb",
                **feat,
            }
        )

    with engine.begin() as conn:
        for row in rows:
            conn.execute(text(UPSERT_SQL), row)
    logger.info("Upserted %d macro calendar rows (%s .. %s)", len(rows), d0, d1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

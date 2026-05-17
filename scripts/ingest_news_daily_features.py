#!/usr/bin/env python3
"""
Backfill news_daily_features from knowledge_base (per symbol, as-of 16:00 ET).

  python scripts/ingest_news_daily_features.py --tickers-source game5m --from-date 2024-01-01 --ensure-table
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
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
    session_day_start_utc,
    trading_dates_from_quotes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO news_daily_features (
  exchange, symbol, trade_date, snapshot_label, snapshot_ts_utc,
  article_count, sentiment_mean, sentiment_min, sentiment_max,
  negative_count, very_negative_count, positive_count,
  kb_rows_used, cutoff_ts_utc, source, updated_at
)
VALUES (
  :exchange, :symbol, :trade_date, :snapshot_label, NOW(),
  :article_count, :sentiment_mean, :sentiment_min, :sentiment_max,
  :negative_count, :very_negative_count, :positive_count,
  :kb_rows_used, :cutoff_ts_utc, :source, NOW()
)
ON CONFLICT (exchange, symbol, trade_date, snapshot_label) DO UPDATE SET
  snapshot_ts_utc = NOW(),
  article_count = EXCLUDED.article_count,
  sentiment_mean = EXCLUDED.sentiment_mean,
  sentiment_min = EXCLUDED.sentiment_min,
  sentiment_max = EXCLUDED.sentiment_max,
  negative_count = EXCLUDED.negative_count,
  very_negative_count = EXCLUDED.very_negative_count,
  positive_count = EXCLUDED.positive_count,
  kb_rows_used = EXCLUDED.kb_rows_used,
  cutoff_ts_utc = EXCLUDED.cutoff_ts_utc,
  source = EXCLUDED.source,
  updated_at = NOW()
"""


def _agg_news_rows(rows: List[Any]) -> Dict[str, Any]:
    scores: List[float] = []
    neg = very_neg = pos = 0
    for row in rows:
        sc = row[0]
        if sc is None:
            continue
        try:
            s = float(sc)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(s):
            continue
        scores.append(s)
        if s < 0.4:
            neg += 1
        if s < 0.35:
            very_neg += 1
        if s > 0.65:
            pos += 1
    n = len(scores)
    if n == 0:
        return {
            "article_count": 0,
            "sentiment_mean": None,
            "sentiment_min": None,
            "sentiment_max": None,
            "negative_count": 0,
            "very_negative_count": 0,
            "positive_count": 0,
            "kb_rows_used": len(rows),
        }
    return {
        "article_count": n,
        "sentiment_mean": float(np.mean(scores)),
        "sentiment_min": float(np.min(scores)),
        "sentiment_max": float(np.max(scores)),
        "negative_count": neg,
        "very_negative_count": very_neg,
        "positive_count": pos,
        "kb_rows_used": len(rows),
    }


def build_rows_for_symbol(
    engine,
    symbol: str,
    trade_dates: List[date],
    *,
    exchange: str,
    snapshot_label: str,
) -> List[Dict[str, Any]]:
    sym = str(symbol).strip().upper()
    out: List[Dict[str, Any]] = []
    with engine.connect() as conn:
        for td in trade_dates:
            cutoff = session_close_utc(td)
            day0 = session_day_start_utc(td)
            res = conn.execute(
                text(
                    """
                    SELECT sentiment_score
                    FROM knowledge_base
                    WHERE UPPER(TRIM(ticker)) = :sym
                      AND content IS NOT NULL
                      AND LENGTH(TRIM(content)) > 5
                      AND COALESCE(ingested_at, ts) >= :day0
                      AND COALESCE(ingested_at, ts) < :cutoff
                    """
                ),
                {"sym": sym, "day0": day0, "cutoff": cutoff},
            ).fetchall()
            agg = _agg_news_rows(res)
            out.append(
                {
                    "exchange": exchange,
                    "symbol": sym,
                    "trade_date": td,
                    "snapshot_label": snapshot_label,
                    "cutoff_ts_utc": cutoff,
                    "source": "knowledge_base_agg",
                    **agg,
                }
            )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest news_daily_features from knowledge_base.")
    p.add_argument("--tickers-source", choices=("game5m", "config", "manual"), default="game5m")
    p.add_argument("tickers", nargs="*", help="With --tickers-source=manual")
    p.add_argument("--from-date", type=str, default="")
    p.add_argument("--to-date", type=str, default="")
    p.add_argument("--period-days", type=int, default=400)
    p.add_argument("--exchange", default="US")
    p.add_argument("--snapshot-label", default="latest")
    p.add_argument("--ensure-table", action="store_true")
    args = p.parse_args()

    engine = create_engine(get_database_url())
    if args.ensure_table:
        apply_sql_migrations(engine, ("023_news_daily_features.sql",))

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
            logger.warning("%s: no quotes dates in range", sym)
            continue
        rows = build_rows_for_symbol(
            engine,
            sym,
            tdates,
            exchange=args.exchange,
            snapshot_label=args.snapshot_label,
        )
        with engine.begin() as conn:
            for row in rows:
                conn.execute(text(UPSERT_SQL), row)
        total += len(rows)
        nz = sum(1 for r in rows if int(r.get("article_count") or 0) > 0)
        logger.info("%s: upserted %d days (%d with articles)", sym, len(rows), nz)

    logger.info("Done: %d rows upserted for %d symbols", total, len(symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

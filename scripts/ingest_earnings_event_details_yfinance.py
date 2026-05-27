#!/usr/bin/env python3
"""
MVP backfill for earnings_event_detail from yfinance earnings_dates.

This intentionally stores reported EPS / surprise in earnings_event_detail but
the pre-event feature builder only uses leakage-safe fields (estimate + timing).

Examples:
  python scripts/ingest_earnings_event_details_yfinance.py --tickers TER,AMD,MU,ASML,MSFT,META,AMZN,INTC,ORCL,ALAB
  python scripts/ingest_earnings_event_details_yfinance.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from sqlalchemy import bindparam, text

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.ticker_groups import get_config_ticker_symbols_upper_unique  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_TICKERS = ("TER", "AMD", "MU", "ASML", "MSFT", "META", "AMZN", "INTC", "ORCL", "ALAB")


def _float_or_none(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _event_date_et(index_value: Any) -> tuple[date, str, int | None, str]:
    ts = pd.Timestamp(index_value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York", ambiguous=True)
    else:
        ts = ts.tz_convert("America/New_York")
    hour = int(ts.hour)
    if hour < 9:
        timing = "BEFORE_OPEN"
    elif hour < 16:
        timing = "DURING_SESSION"
    else:
        timing = "AFTER_CLOSE"
    return ts.date(), ts.isoformat(), hour, timing


def _load_existing_event_rows(engine, *, dataset_version: str, tickers: Iterable[str]) -> Dict[tuple[str, date], list[dict]]:
    symbols = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    if not symbols:
        return {}
    q = text(
        """
        SELECT
          e.knowledge_base_id,
          e.symbol,
          e.event_time_et::date AS event_date,
          e.event_time_et::text AS event_time_et,
          e.event_type
        FROM event_reaction_dataset e
        WHERE e.dataset_version = :dv
          AND e.knowledge_base_id IS NOT NULL
          AND UPPER(TRIM(e.symbol)) IN :symbols
          AND UPPER(COALESCE(e.event_type, '')) LIKE '%EARNING%'
        ORDER BY e.event_time_et DESC, e.id DESC
        """
    )
    out: Dict[tuple[str, date], list[dict]] = {}
    q = q.bindparams(bindparam("symbols", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(q, {"dv": dataset_version, "symbols": symbols}).mappings().all()
    for r in rows:
        d = r.get("event_date")
        if d is None:
            continue
        key = (str(r["symbol"]).strip().upper(), d)
        out.setdefault(key, []).append(dict(r))
    return out


def _fetch_yfinance_earnings_dates(ticker: str) -> pd.DataFrame:
    import yfinance as yf

    df = yf.Ticker(ticker).earnings_dates
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def _upsert_detail(
    engine,
    *,
    knowledge_base_id: int,
    ticker: str,
    fiscal_period: str,
    eps_actual: Optional[float],
    eps_estimate: Optional[float],
    eps_surprise_pct: Optional[float],
    earnings_date_et: str,
    report_hour_et: Optional[int],
    report_timing: str,
    dry_run: bool,
) -> None:
    guidance_summary = {
        "source": "yfinance.earnings_dates",
        "source_kind": "structured_provider",
        "earnings_date_et": earnings_date_et,
        "report_hour_et": report_hour_et,
        "report_timing": report_timing,
        "eps_surprise_pct": eps_surprise_pct,
        "has_reported_eps": eps_actual is not None,
        "leakage_note": "reported EPS / surprise are post-release facts; pre-event features use estimate/timing only",
    }
    params = {
        "knowledge_base_id": int(knowledge_base_id),
        "fiscal_period": fiscal_period,
        "eps_actual": eps_actual,
        "eps_estimate": eps_estimate,
        "guidance_summary": json.dumps(guidance_summary, ensure_ascii=False),
        "affected_tickers": json.dumps([ticker], ensure_ascii=False),
    }
    if dry_run:
        logger.info("dry-run upsert kb=%s %s %s", knowledge_base_id, ticker, guidance_summary)
        return
    q = text(
        """
        INSERT INTO earnings_event_detail (
          knowledge_base_id, fiscal_period, eps_actual, eps_estimate,
          revenue_actual, revenue_estimate, guidance_summary, affected_tickers,
          updated_at
        )
        VALUES (
          :knowledge_base_id, :fiscal_period, :eps_actual, :eps_estimate,
          NULL, NULL, CAST(:guidance_summary AS jsonb), CAST(:affected_tickers AS jsonb),
          NOW()
        )
        ON CONFLICT (knowledge_base_id) DO UPDATE SET
          fiscal_period = EXCLUDED.fiscal_period,
          eps_actual = EXCLUDED.eps_actual,
          eps_estimate = EXCLUDED.eps_estimate,
          guidance_summary = EXCLUDED.guidance_summary,
          affected_tickers = EXCLUDED.affected_tickers,
          updated_at = NOW()
        """
    )
    with engine.begin() as conn:
        conn.execute(q, params)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill earnings_event_detail from yfinance earnings_dates")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS), help="Comma-separated ticker list")
    ap.add_argument("--dataset-version", default="v0")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max upserts, 0 = no limit")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    allowed = set(get_config_ticker_symbols_upper_unique())
    tickers = [t for t in tickers if t in allowed]
    if not tickers:
        logger.error("No tickers left after config-universe filter")
        return 1

    engine = get_engine()
    existing = _load_existing_event_rows(engine, dataset_version=args.dataset_version.strip() or "v0", tickers=tickers)
    logger.info("Loaded %d event_reaction_dataset date keys for %d tickers", len(existing), len(tickers))
    upserts = 0
    misses = 0
    errors = 0
    for ticker in tickers:
        try:
            df = _fetch_yfinance_earnings_dates(ticker)
        except Exception as e:
            errors += 1
            logger.warning("%s: yfinance earnings_dates error: %s", ticker, e)
            continue
        if df.empty:
            logger.info("%s: no earnings_dates", ticker)
            continue
        for idx, row in df.iterrows():
            event_d, earnings_date_et, report_hour_et, report_timing = _event_date_et(idx)
            matches = existing.get((ticker, event_d)) or []
            if not matches:
                misses += 1
                continue
            eps_est = _float_or_none(row.get("EPS Estimate"))
            eps_rep = _float_or_none(row.get("Reported EPS"))
            eps_sur = _float_or_none(row.get("Surprise(%)"))
            for match in matches[:1]:
                _upsert_detail(
                    engine,
                    knowledge_base_id=int(match["knowledge_base_id"]),
                    ticker=ticker,
                    fiscal_period=str(event_d),
                    eps_actual=eps_rep,
                    eps_estimate=eps_est,
                    eps_surprise_pct=eps_sur,
                    earnings_date_et=earnings_date_et,
                    report_hour_et=report_hour_et,
                    report_timing=report_timing,
                    dry_run=bool(args.dry_run),
                )
                upserts += 1
                if args.limit and upserts >= args.limit:
                    logger.info("Limit reached: %d", args.limit)
                    logger.info("Done: upserts=%d misses=%d errors=%d dry_run=%s", upserts, misses, errors, args.dry_run)
                    return 0
    logger.info("Done: upserts=%d misses=%d errors=%d dry_run=%s", upserts, misses, errors, args.dry_run)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

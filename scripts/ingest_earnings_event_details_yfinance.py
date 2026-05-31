#!/usr/bin/env python3
"""
MVP backfill for earnings_event_detail from yfinance earnings_dates.

This intentionally stores reported EPS / surprise in earnings_event_detail but
the pre-event feature builder only uses leakage-safe fields (estimate + timing).

Examples:
  python scripts/ingest_earnings_event_details_yfinance.py --from-config-equities --ensure-kb-events --earnings-limit 40
  python scripts/ingest_earnings_event_details_yfinance.py --tickers TER,AMD,MU,ASML,MSFT,META,AMZN,INTC,ORCL,ALAB
  python scripts/ingest_earnings_event_details_yfinance.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from sqlalchemy import bindparam, text

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_intelligence_universe import get_earnings_intelligence_universe  # noqa: E402
from services.ticker_groups import get_config_ticker_symbols_upper_unique  # noqa: E402
from services.yfinance_earnings_fetcher import YFINANCE_EARNINGS_SOURCE  # noqa: E402

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


def _is_equity_symbol(ticker: str) -> bool:
    t = str(ticker or "").strip().upper()
    return bool(t) and not t.startswith("^") and "=" not in t and t not in ("MACRO", "US_MACRO")


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


def _fetch_yfinance_earnings_dates(ticker: str, *, limit: int) -> pd.DataFrame:
    import yfinance as yf

    yt = yf.Ticker(ticker)
    df = None
    try:
        if hasattr(yt, "get_earnings_dates"):
            df = yt.get_earnings_dates(limit=max(1, int(limit)))
    except Exception as e:
        logger.debug("%s: get_earnings_dates error: %s", ticker, e)
        df = None
    if df is None:
        df = yt.earnings_dates
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        if len(df) > int(limit):
            df = df.iloc[: int(limit)]
    except Exception:
        pass
    return df


def _kb_report_datetime(event_d: date) -> datetime:
    return datetime(int(event_d.year), int(event_d.month), int(event_d.day))


def _find_kb_earnings_row(engine, ticker: str, event_d: date) -> Optional[int]:
    q = text(
        """
        SELECT id
        FROM knowledge_base
        WHERE UPPER(TRIM(ticker)) = :ticker
          AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
          AND ts::date = :event_d
        ORDER BY id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(q, {"ticker": ticker.strip().upper(), "event_d": event_d}).fetchone()
    return int(row[0]) if row else None


def _ensure_kb_earnings_row(
    engine,
    *,
    ticker: str,
    event_d: date,
    eps_estimate: Optional[float],
    dry_run: bool,
) -> Optional[int]:
    existing = _find_kb_earnings_row(engine, ticker, event_d)
    if existing:
        return existing
    content = f"Earnings date (Yahoo/yfinance) for {ticker}"
    if eps_estimate is not None:
        content += f"\nEPS estimate: {eps_estimate} USD"
    if dry_run:
        logger.info("dry-run insert KB earnings %s %s", ticker, event_d)
        return None
    q = text(
        """
        INSERT INTO knowledge_base (ts, ticker, source, content, event_type, importance)
        VALUES (:ts, :ticker, :source, :content, 'EARNINGS', 'HIGH')
        RETURNING id
        """
    )
    with engine.begin() as conn:
        row = conn.execute(
            q,
            {
                "ts": _kb_report_datetime(event_d),
                "ticker": ticker.strip().upper(),
                "source": YFINANCE_EARNINGS_SOURCE,
                "content": content,
            },
        ).fetchone()
    return int(row[0]) if row else None


def _detail_has_llm_extraction(engine, knowledge_base_id: int) -> bool:
    q = text(
        """
        SELECT guidance_summary
        FROM earnings_event_detail
        WHERE knowledge_base_id = :kb_id
        """
    )
    with engine.connect() as conn:
        row = conn.execute(q, {"kb_id": int(knowledge_base_id)}).first()
    if not row or not row[0]:
        return False
    gs = row[0]
    if isinstance(gs, str):
        try:
            gs = json.loads(gs)
        except Exception:
            return False
    return bool(isinstance(gs, dict) and gs.get("extraction_meta"))


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
    if _detail_has_llm_extraction(engine, int(knowledge_base_id)):
        q = text(
            """
            UPDATE earnings_event_detail
            SET
              fiscal_period = :fiscal_period,
              eps_actual = :eps_actual,
              eps_estimate = :eps_estimate,
              updated_at = NOW()
            WHERE knowledge_base_id = :knowledge_base_id
            """
        )
        with engine.begin() as conn:
            conn.execute(
                q,
                {
                    "knowledge_base_id": int(knowledge_base_id),
                    "fiscal_period": fiscal_period,
                    "eps_actual": eps_actual,
                    "eps_estimate": eps_estimate,
                },
            )
        logger.debug("%s kb=%s yfinance refresh: preserved LLM extraction_meta", ticker, knowledge_base_id)
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
          guidance_summary = COALESCE(earnings_event_detail.guidance_summary, '{}'::jsonb) || EXCLUDED.guidance_summary,
          affected_tickers = EXCLUDED.affected_tickers,
          updated_at = NOW()
        """
    )
    with engine.begin() as conn:
        conn.execute(q, params)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill earnings_event_detail from yfinance earnings_dates")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS), help="Comma-separated ticker list")
    ap.add_argument("--from-config-equities", action="store_true", help="Use FAST+MEDIUM+LONG equity symbols from config")
    ap.add_argument("--ensure-kb-events", action="store_true", help="Insert missing knowledge_base EARNINGS rows before detail upsert")
    ap.add_argument("--dataset-version", default="v0")
    ap.add_argument("--earnings-limit", type=int, default=25, help="Max yfinance earnings dates per ticker")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max upserts, 0 = no limit")
    args = ap.parse_args()

    allowed = set(get_config_ticker_symbols_upper_unique()) | set(get_earnings_intelligence_universe())
    if args.from_config_equities:
        tickers = [t for t in sorted(allowed) if _is_equity_symbol(t)]
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    tickers = [t for t in tickers if t in allowed]
    if not tickers:
        logger.error("No tickers left after config-universe filter")
        return 1

    engine = get_engine()
    existing = {} if args.ensure_kb_events else _load_existing_event_rows(engine, dataset_version=args.dataset_version.strip() or "v0", tickers=tickers)
    logger.info("Loaded %d event_reaction_dataset date keys for %d tickers", len(existing), len(tickers))
    upserts = 0
    misses = 0
    errors = 0
    kb_inserted_or_found = 0
    yf_rows = 0
    for ticker in tickers:
        try:
            df = _fetch_yfinance_earnings_dates(ticker, limit=max(1, min(int(args.earnings_limit), 100)))
        except Exception as e:
            errors += 1
            logger.warning("%s: yfinance earnings_dates error: %s", ticker, e)
            continue
        if df.empty:
            logger.info("%s: no earnings_dates", ticker)
            continue
        yf_rows += len(df)
        logger.info("%s: yfinance rows=%s", ticker, len(df))
        for idx, row in df.iterrows():
            event_d, earnings_date_et, report_hour_et, report_timing = _event_date_et(idx)
            eps_est = _float_or_none(row.get("EPS Estimate"))
            eps_rep = _float_or_none(row.get("Reported EPS"))
            eps_sur = _float_or_none(row.get("Surprise(%)"))
            if args.ensure_kb_events:
                kb_id = _ensure_kb_earnings_row(engine, ticker=ticker, event_d=event_d, eps_estimate=eps_est, dry_run=bool(args.dry_run))
                if kb_id is None:
                    misses += 1
                    continue
                matches = [{"knowledge_base_id": kb_id}]
                kb_inserted_or_found += 1
            else:
                matches = existing.get((ticker, event_d)) or []
                if not matches:
                    misses += 1
                    continue
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
                    logger.info(
                        "Done: yfinance_rows=%d kb_found_or_inserted=%d upserts=%d misses=%d errors=%d dry_run=%s",
                        yf_rows, kb_inserted_or_found, upserts, misses, errors, args.dry_run,
                    )
                    return 0
    logger.info(
        "Done: yfinance_rows=%d kb_found_or_inserted=%d upserts=%d misses=%d errors=%d dry_run=%s",
        yf_rows, kb_inserted_or_found, upserts, misses, errors, args.dry_run,
    )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

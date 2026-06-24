#!/usr/bin/env python3
"""
Снимок OI по страйкам (Polygon) → options_chain_oi_snapshot.

Cron (после миграции 031): 1×/день после US close, watchlist тикеров.
  docker exec lse-bot python scripts/snapshot_options_chain_oi.py --ticker MU

Phase 3: ползунок «как смещались плиты» читает историю из БД.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from typing import Any, Dict, List

from services.options_tickers import get_options_oi_watchlist


def snapshot_ticker(ticker: str, *, expiration_date: str | None, dry_run: bool) -> Dict[str, Any]:
    from services.polygon_options import fetch_option_expiration_dates, fetch_options_chain_snapshot

    sym = ticker.strip().upper()
    exps = fetch_option_expiration_dates(sym)
    exp = expiration_date or (exps[0] if exps else None)
    if not exp:
        return {"ticker": sym, "status": "error", "error": "no expirations"}

    raw = fetch_options_chain_snapshot(sym, expiration_date=exp)
    if raw.get("status") == "error":
        return {"ticker": sym, "status": "error", "error": raw.get("error")}

    contracts: List[Dict[str, Any]] = raw.get("contracts") or []
    rows = [
        {
            "snapshot_date": date.today().isoformat(),
            "ticker": sym,
            "expiration_date": exp,
            "spot": raw.get("underlying_price"),
            "strike": c["strike"],
            "contract_type": c["contract_type"],
            "open_interest": int(c.get("open_interest") or 0),
            "volume": int(c.get("volume") or 0),
            "source": "polygon",
        }
        for c in contracts
        if int(c.get("open_interest") or 0) > 0 or int(c.get("volume") or 0) > 0
    ]

    if dry_run:
        return {
            "ticker": sym,
            "status": "ok",
            "expiration_date": exp,
            "rows": len(rows),
            "dry_run": True,
        }

    try:
        from sqlalchemy import text
        from report_generator import get_engine

        engine = get_engine()
        q = text(
            """
            INSERT INTO options_chain_oi_snapshot (
                snapshot_date, ticker, expiration_date, spot, strike,
                contract_type, open_interest, volume, source
            ) VALUES (
                :snapshot_date, :ticker, :expiration_date, :spot, :strike,
                :contract_type, :open_interest, :volume, :source
            )
            ON CONFLICT (snapshot_date, ticker, expiration_date, strike, contract_type)
            DO UPDATE SET
                open_interest = EXCLUDED.open_interest,
                volume = EXCLUDED.volume,
                spot = EXCLUDED.spot,
                created_at = NOW()
            """
        )
        with engine.begin() as conn:
            for row in rows:
                conn.execute(q, row)
    except Exception as e:
        return {"ticker": sym, "status": "error", "error": str(e), "hint": "run migration 031_options_chain_oi_snapshot.sql"}

    return {"ticker": sym, "status": "ok", "expiration_date": exp, "rows": len(rows)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot Polygon option OI to PostgreSQL")
    ap.add_argument("--ticker", action="append", default=[], help="repeatable; default watchlist")
    ap.add_argument("--expiration", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.ticker] if args.ticker else get_options_oi_watchlist()
    results = []
    for i, t in enumerate(tickers):
        if i > 0:
            time.sleep(0.75)
        results.append(snapshot_ticker(t, expiration_date=args.expiration, dry_run=args.dry_run))
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"{r.get('ticker')}: {r.get('status')} rows={r.get('rows', '—')} {r.get('error', '')}")
    return 0 if all(r.get("status") == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

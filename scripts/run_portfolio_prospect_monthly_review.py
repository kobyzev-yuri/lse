#!/usr/bin/env python3
"""
Monthly portfolio prospectivity / allocation review (~6m path + 20d overlay).

Writes last_portfolio_prospect_monthly_review.json for /portfolio/daily UI
(report above charts) and ops. Cron: 1st of month 07:20 MSK.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Monthly portfolio prospectivity / allocation review")
    ap.add_argument("--lookback-days", type=int, default=126, help="Trading days (~6m default)")
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.portfolio_card import get_portfolio_trade_tickers
    from services.portfolio_prospect_monthly import (
        build_portfolio_prospect_monthly_report,
        default_monthly_review_path,
        write_monthly_review_artifact,
    )

    eng = get_engine()
    tickers = list(get_portfolio_trade_tickers() or [])
    payload = build_portfolio_prospect_monthly_report(
        tickers,
        engine=eng,
        lookback_trading_days=int(args.lookback_days),
    )
    out = Path(args.out).expanduser() if (args.out or "").strip() else default_monthly_review_path()
    write_monthly_review_artifact(payload, out)
    buckets = payload.get("bucket_counts") or {}
    focus = [r["ticker"] for r in (payload.get("invest_first") or [])]
    logger.info("Wrote %s | buckets=%s invest_first=%s", out, buckets, focus)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

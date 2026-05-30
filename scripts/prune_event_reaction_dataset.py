#!/usr/bin/env python3
"""
Remove event_reaction_dataset rows that cannot be feature-built (e.g. pre-listing quotes).

Examples:
  python scripts/prune_event_reaction_dataset.py --symbol NBIS --before-date 2024-10-25 --dry-run
  python scripts/prune_event_reaction_dataset.py --symbol NBIS --before-date 2024-10-25
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune event_reaction_dataset rows before a cutoff date")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--symbol", required=True, help="Ticker to prune")
    ap.add_argument("--before-date", required=True, help="Delete rows with event_time_et date strictly before YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sym = args.symbol.strip().upper()
    cutoff = _parse_date(args.before_date)
    dv = args.dataset_version.strip()

    engine = get_engine()
    count_q = text(
        """
        SELECT COUNT(*)
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND UPPER(TRIM(symbol)) = :sym
          AND event_time_et::date < :cutoff
        """
    )
    delete_q = text(
        """
        DELETE FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND UPPER(TRIM(symbol)) = :sym
          AND event_time_et::date < :cutoff
        """
    )
    params = {"dv": dv, "sym": sym, "cutoff": cutoff}

    with engine.connect() as conn:
        n = int(conn.execute(count_q, params).scalar() or 0)
    if n == 0:
        logger.info("Nothing to prune for %s before %s (dataset_version=%s)", sym, cutoff, dv)
        return 0

    if args.dry_run:
        logger.info("dry-run: would delete %s rows (%s before %s, dv=%s)", n, sym, cutoff, dv)
        return 0

    with engine.begin() as conn:
        conn.execute(delete_q, params)
    logger.info("Deleted %s rows (%s before %s, dataset_version=%s)", n, sym, cutoff, dv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

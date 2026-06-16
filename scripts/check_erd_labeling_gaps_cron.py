#!/usr/bin/env python3
"""
Audit ERD labeling gaps (no_quotes / anchor_unresolved) and optional Telegram alert.

Cron (after nightly ERD backfill):
  python scripts/check_erd_labeling_gaps_cron.py --execute --telegram
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
    ap = argparse.ArgumentParser(description="ERD labeling gap audit + alert")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--execute", action="store_true", help="Write alert JSON")
    ap.add_argument("--telegram", action="store_true", help="Send Telegram if over threshold")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.erd_labeling_gaps import (
        audit_erd_labeling_gaps,
        maybe_send_erd_labeling_gap_telegram,
        write_erd_labeling_gap_alert,
    )

    engine = get_engine()
    payload = audit_erd_labeling_gaps(
        engine,
        dataset_version=args.dataset_version.strip(),
        limit=max(1, int(args.limit)),
    )
    logger.info(
        "ERD gaps: no_quotes=%s anchor_unresolved=%s future_excluded=%s over_threshold=%s",
        payload.get("no_quotes"),
        payload.get("anchor_unresolved"),
        payload.get("future_events_excluded"),
        payload.get("over_threshold"),
    )

    if args.execute:
        path = write_erd_labeling_gap_alert(payload, project_root=project_root)
        logger.info("Wrote %s", path)

    if args.telegram and payload.get("alert_active"):
        if maybe_send_erd_labeling_gap_telegram(payload):
            logger.info("Telegram alert sent")
        else:
            logger.warning("Telegram alert not sent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

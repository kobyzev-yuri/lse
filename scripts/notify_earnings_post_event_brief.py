#!/usr/bin/env python3
"""Cron: Telegram brief for newly extracted past earnings events (Phase C)."""
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
    ap = argparse.ArgumentParser(description="Notify Telegram on new earnings event briefs")
    ap.add_argument("--lookback-days", type=int, default=3)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.earnings_post_event_brief_alert import notify_new_post_event_briefs

    result = notify_new_post_event_briefs(
        get_engine(),
        project_root=project_root,
        lookback_days=args.lookback_days,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    logger.info("Post-event brief notify: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

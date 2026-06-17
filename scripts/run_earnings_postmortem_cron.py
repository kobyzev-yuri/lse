#!/usr/bin/env python3
"""Rebuild earnings post-mortem JSONL + trust metrics + unified arbiter artifact."""
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
    ap = argparse.ArgumentParser(description="Earnings post-mortem + trust metrics refresh")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--skip-arbiter", action="store_true")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.earnings_event_postmortem import refresh_earnings_postmortem
    from services.unified_trust_arbiter import write_unified_trust_arbiter

    result = refresh_earnings_postmortem(
        get_engine(),
        project_root=project_root,
        dataset_version=args.dataset_version.strip(),
        since=args.since.strip(),
    )
    logger.info(
        "Post-mortem: n_rows=%s metrics=%s",
        result.get("n_postmortem_rows"),
        (result.get("metrics") or {}).get("contours"),
    )

    if not args.skip_arbiter:
        arb = write_unified_trust_arbiter(project_root=project_root)
        logger.info("Trust arbiter written: %s", arb.get("path"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

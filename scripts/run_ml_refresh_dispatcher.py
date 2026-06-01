#!/usr/bin/env python3
"""
Poll all registered ML contours: run refresh scripts when due; write ml_contours_status.json.

  python scripts/run_ml_refresh_dispatcher.py
  python scripts/run_ml_refresh_dispatcher.py --contour open_path
  python scripts/run_ml_refresh_dispatcher.py --slot nightly --force-full
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import List

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Contours with implemented refresh scripts (others: registry-only / manual train).
ACTIVE_REFRESH_CONTOURS: tuple[str, ...] = (
    "open_path",
    "earnings_grid",
    "game5m_entry",
    "portfolio",
    "event_reaction_regression",
)

NIGHTLY_FULL_CONTOURS: frozenset[str] = frozenset({"earnings_grid", "open_path", "event_reaction_regression"})
WEEKLY_FULL_CONTOURS: frozenset[str] = frozenset({"open_path", "game5m_entry"})


def _run_script(script: Path, *, extra_args: List[str]) -> int:
    if not script.is_file():
        logger.warning("skip missing script %s", script)
        return 0
    cmd = [sys.executable, str(script)] + extra_args
    logger.info("dispatcher: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(project_root))


def main() -> int:
    ap = argparse.ArgumentParser(description="Unified ML refresh dispatcher")
    ap.add_argument("--contour", default="all", help="contour_id or all")
    ap.add_argument("--slot", choices=("poll", "nightly", "weekly_full"), default="poll")
    ap.add_argument("--force-full", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Only rebuild ml_contours_status.json")
    args = ap.parse_args()

    from services.ml_contour_deltas import build_delta_resolver, build_readiness_resolver
    from services.ml_contour_refresh import ML_CONTOUR_REGISTRY, collect_aggregate_contours_status, get_contour_spec

    if args.contour == "all":
        contour_ids = list(ACTIVE_REFRESH_CONTOURS)
    else:
        contour_ids = [args.contour.strip().lower()]

    if not args.dry_run:
        for cid in contour_ids:
            spec = get_contour_spec(cid)
            script = project_root / spec.refresh_script
            extra: List[str] = []
            if args.force_full or args.slot == "weekly_full" or (
                args.slot == "nightly" and cid in NIGHTLY_FULL_CONTOURS
            ):
                extra.append("--full")
            rc = _run_script(script, extra_args=extra)
            if rc != 0:
                logger.warning("contour %s refresh exit %s", cid, rc)

    status = collect_aggregate_contours_status(
        project_root,
        readiness_resolver=build_readiness_resolver(project_root),
        delta_resolver=build_delta_resolver(project_root),
    )
    logger.info(
        "ml_contours_status.json updated: %s contours",
        len(status.get("contours") or []),
    )
    for row in status.get("contours") or []:
        if row.get("contour_id") in contour_ids or args.contour == "all":
            tr = row.get("trigger") or {}
            logger.info(
                "  %s phase=%s train=%s apply=%s reasons=%s",
                row.get("contour_id"),
                row.get("phase"),
                tr.get("should_train"),
                tr.get("should_apply_data"),
                tr.get("reasons"),
            )

    missing = set(ML_CONTOUR_REGISTRY) - set(ACTIVE_REFRESH_CONTOURS)
    if missing:
        logger.debug("registry-only contours (no auto refresh yet): %s", sorted(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

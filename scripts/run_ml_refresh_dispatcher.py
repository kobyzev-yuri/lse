#!/usr/bin/env python3
"""
Poll all registered ML contours: run refresh scripts when due; write ml_contours_status.json.

  python scripts/run_ml_refresh_dispatcher.py
  python scripts/run_ml_refresh_dispatcher.py --contour open_path
  python scripts/run_ml_refresh_dispatcher.py --slot nightly
  python scripts/run_ml_refresh_dispatcher.py --slot weekly_full
  python scripts/run_ml_refresh_dispatcher.py --contour multiday_lr --full

Per-contour lock: /tmp/lse_ml_refresh_<contour_id>.lock (flock -n when available).
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Contours with implemented refresh scripts (others: registry-only / manual train).
ACTIVE_REFRESH_CONTOURS: tuple[str, ...] = (
    "open_path",
    "earnings_grid",
    "game5m_entry",
    "game5m_entry_bar_v2",
    "portfolio",
    "event_reaction_regression",
    "multiday_lr",
    "recovery",
    "gap_forecast",
)

# Nightly slot (after ERD build + open-path labels cron). Order matters.
NIGHTLY_SEQUENCE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("game5m_entry", ("--full",)),
    ("open_path", ("--apply-data", "--incremental-train", "--skip-labels")),
    ("event_reaction_regression", ("--full",)),
    ("earnings_grid", ("--full",)),
    ("portfolio", ()),
    ("gap_forecast", ("--full",)),
)

WEEKLY_FULL_CONTOURS: frozenset[str] = frozenset(
    {"open_path", "game5m_entry", "game5m_entry_bar_v2", "multiday_lr", "recovery", "gap_forecast"}
)

_FLOCK_BIN: str | None = shutil.which("flock")


def _contour_lock_path(contour_id: str) -> str:
    return f"/tmp/lse_ml_refresh_{contour_id}.lock"


def _run_script(script: Path, *, extra_args: List[str], contour_id: str) -> int:
    if not script.is_file():
        logger.warning("skip missing script %s", script)
        return 0
    base_cmd = [sys.executable, str(script)] + extra_args
    if _FLOCK_BIN:
        cmd = [_FLOCK_BIN, "-n", _contour_lock_path(contour_id)] + base_cmd
    else:
        logger.debug("flock not found; running without per-contour lock")
        cmd = base_cmd
    logger.info("dispatcher: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(project_root))


def _contours_for_slot(slot: str, contour_filter: str) -> List[Tuple[str, List[str]]]:
    if contour_filter != "all":
        cid = contour_filter.strip().lower()
        return [(cid, [])]

    if slot == "nightly":
        return [(cid, list(extra)) for cid, extra in NIGHTLY_SEQUENCE]

    if slot == "weekly_full":
        return [(cid, ["--full"] if cid in WEEKLY_FULL_CONTOURS else []) for cid in ACTIVE_REFRESH_CONTOURS]

    # poll: all active contours, data-driven triggers inside each script
    return [(cid, []) for cid in ACTIVE_REFRESH_CONTOURS]


def main() -> int:
    ap = argparse.ArgumentParser(description="Unified ML refresh dispatcher")
    ap.add_argument("--contour", default="all", help="contour_id or all")
    ap.add_argument("--slot", choices=("poll", "nightly", "weekly_full"), default="poll")
    ap.add_argument("--force-full", "--full", action="store_true", dest="force_full")
    ap.add_argument("--dry-run", action="store_true", help="Only rebuild ml_contours_status.json")
    args = ap.parse_args()

    from services.ml_contour_deltas import build_delta_resolver, build_readiness_resolver
    from services.ml_contour_refresh import ML_CONTOUR_REGISTRY, collect_aggregate_contours_status, get_contour_spec

    work = _contours_for_slot(args.slot, args.contour)

    if not args.dry_run:
        for cid, preset_args in work:
            spec = get_contour_spec(cid)
            script = project_root / spec.refresh_script
            extra: List[str] = list(preset_args)
            if args.force_full and "--full" not in extra:
                extra.append("--full")
            rc = _run_script(script, extra_args=extra, contour_id=cid)
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
    ran_ids = {cid for cid, _ in work}
    for row in status.get("contours") or []:
        cid = row.get("contour_id")
        if cid in ran_ids or args.contour == "all":
            tr = row.get("trigger") or {}
            logger.info(
                "  %s phase=%s train=%s apply=%s reasons=%s",
                cid,
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

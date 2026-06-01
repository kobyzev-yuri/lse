#!/usr/bin/env python3
"""Event-reaction regression refresh with data-driven trigger (new labeled ERD rows)."""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _default_q_dir() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    return project_root / "local" / "logs" / "ml_data_quality"


def main() -> int:
    ap = argparse.ArgumentParser(description="Event-reaction regression ML refresh")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.ml_contour_runner import finalize_contour_refresh, plan_contour_refresh

    trigger, _g, deltas = plan_contour_refresh(
        "event_reaction_regression",
        project_root,
        get_engine(),
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("Event-reaction refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root,
            "event_reaction_regression",
            trigger,
            apply_ran=False,
            train_ran=False,
            full=False,
            skipped=True,
        )
        return 0

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    do_train = trigger.should_train or full_train
    train_dry_run = args.dry_run or not do_train or (not full_train and mode == "dry_run")

    q_dir = _default_q_dir()
    metrics_path = q_dir / "last_event_reaction_train_metrics.json"
    py = sys.executable
    train_cmd = [py, str(project_root / "scripts/train_event_reaction_catboost.py")]
    dv = (get_config_value("EVENT_REACTION_DATASET_VERSION") or "").strip()
    fbv = (get_config_value("EVENT_REACTION_FEATURE_BUILDER_VERSION") or "").strip()
    if dv:
        train_cmd += ["--dataset-version", dv]
    if fbv:
        train_cmd += ["--feature-builder-version", fbv]
    train_cmd += ["--json-metrics-out", str(metrics_path)]
    if train_dry_run:
        train_cmd.append("--dry-run")
    train_rc = subprocess.call(train_cmd, cwd=str(project_root))

    finalize_contour_refresh(
        project_root,
        "event_reaction_regression",
        trigger,
        apply_ran=args.apply_data or trigger.should_apply_data,
        train_ran=do_train and not train_dry_run,
        full=full_train,
        extra={"train_rc": train_rc},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

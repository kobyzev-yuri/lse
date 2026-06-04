#!/usr/bin/env python3
"""Event-reaction regression refresh with data-driven trigger (new labeled ERD rows)."""
from __future__ import annotations

import argparse
import logging
import os
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


def _run_backfill(
    *,
    py: str,
    dataset_version: str,
    feature_builder_version: str,
    limit: int,
    dry_run: bool,
) -> int:
    cmd = [
        py,
        str(project_root / "scripts/backfill_event_reaction_labeling.py"),
        "--dataset-version",
        dataset_version,
        "--include-all-symbols",
        "--limit",
        str(limit),
    ]
    if dry_run:
        cmd.append("--dry-run")
    env = os.environ.copy()
    env["EVENT_REACTION_FEATURE_BUILDER_VERSION"] = feature_builder_version
    logger.info("event-reaction backfill: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(project_root), env=env)


def main() -> int:
    ap = argparse.ArgumentParser(description="Event-reaction regression ML refresh")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--skip-backfill", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.event_reaction_labeling import resolve_training_feature_builder_version
    from services.ml_contour_runner import (
        finalize_contour_refresh,
        plan_contour_refresh,
        should_write_catboost_model,
    )

    engine = get_engine()
    trigger, _gates, deltas = plan_contour_refresh(
        "event_reaction_regression",
        project_root,
        engine,
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
    writes_cbm = should_write_catboost_model(
        cli_dry_run=args.dry_run,
        do_train=do_train,
        full_train=full_train,
        readiness_train_mode=mode,
        phase=trigger.phase,
        continuous_enabled=False,
    )
    train_dry_run = not writes_cbm

    dv = (get_config_value("EVENT_REACTION_DATASET_VERSION") or "").strip() or "v0_expanded_baseline"
    fbv_pref = (get_config_value("EVENT_REACTION_FEATURE_BUILDER_VERSION") or "").strip()
    fbv = resolve_training_feature_builder_version(
        engine,
        dataset_version=dv,
        preferred=fbv_pref,
    )

    apply_data = args.apply_data or trigger.should_apply_data or full_train
    backfill_ran = False
    py = sys.executable
    if apply_data and not args.skip_backfill and not args.dry_run:
        limit = int((get_config_value("EVENT_REACTION_REFRESH_BACKFILL_LIMIT") or "8000").strip() or "8000")
        backfill_rc = _run_backfill(
            py=py,
            dataset_version=dv,
            feature_builder_version=fbv,
            limit=limit,
            dry_run=False,
        )
        backfill_ran = backfill_rc == 0
        if backfill_rc != 0:
            logger.warning("event-reaction backfill exit %s", backfill_rc)

    q_dir = _default_q_dir()
    metrics_path = q_dir / "last_event_reaction_train_metrics.json"
    train_cmd = [py, str(project_root / "scripts/train_event_reaction_catboost.py")]
    train_cmd += ["--dataset-version", dv, "--feature-builder-version", fbv]
    train_cmd += ["--json-metrics-out", str(metrics_path)]
    if train_dry_run:
        train_cmd.append("--dry-run")
    logger.info("event-reaction train: %s", " ".join(train_cmd))
    train_rc = subprocess.call(train_cmd, cwd=str(project_root)) if do_train else 0

    finalize_contour_refresh(
        project_root,
        "event_reaction_regression",
        trigger,
        apply_ran=backfill_ran or (args.apply_data and trigger.should_apply_data),
        train_ran=writes_cbm and train_rc == 0,
        full=full_train,
        extra={"train_rc": train_rc, "feature_builder_version": fbv, "backfill_ran": backfill_ran},
    )
    return 0 if train_rc == 0 else train_rc


if __name__ == "__main__":
    raise SystemExit(main())

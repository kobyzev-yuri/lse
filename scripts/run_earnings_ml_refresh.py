#!/usr/bin/env python3
"""
Refresh earnings ML stack: scenario labels → earnings_v1 features → scenario classifier.

Examples:
  python scripts/run_earnings_ml_refresh.py --dry-run
  python scripts/run_earnings_ml_refresh.py --backfill-limit 200
"""
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


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    logger.info("run: %s", " ".join(cmd))
    import os

    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.call(cmd, env=full_env, cwd=str(project_root))


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings ML refresh pipeline")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--backfill-limit", type=int, default=300)
    ap.add_argument("--skip-labels", action="store_true")
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    args = ap.parse_args()

    py = sys.executable
    if not args.skip_labels:
        label_cmd = [py, "scripts/apply_earnings_scenario_labels.py", "--dataset-version", args.dataset_version]
        if args.dry_run:
            label_cmd.append("--dry-run")
        rc = _run(label_cmd)
        if rc != 0:
            return rc

    if not args.skip_backfill:
        backfill_cmd = [
            py,
            "scripts/backfill_event_reaction_labeling.py",
            "--dataset-version",
            args.dataset_version,
            "--only-features",
            "--force-features",
            "--include-all-symbols",
            "--limit",
            str(max(1, args.backfill_limit)),
        ]
        if args.dry_run:
            backfill_cmd.append("--dry-run")
        rc = _run(
            backfill_cmd,
            env={"EVENT_REACTION_FEATURE_BUILDER_VERSION": "quotes_regime_earnings_v1"},
        )
        if rc != 0:
            return rc

    if not args.skip_train:
        train_cmd = [
            py,
            "scripts/train_event_reaction_scenario_classifier.py",
            "--dataset-version",
            args.dataset_version,
            "--feature-builder-version",
            "quotes_regime_earnings_v1",
        ]
        if args.dry_run:
            train_cmd.append("--dry-run")
        rc = _run(train_cmd)
        if rc != 0:
            return rc

    logger.info("Earnings ML refresh completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

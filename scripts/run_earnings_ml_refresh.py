#!/usr/bin/env python3
"""
Refresh earnings ML stack: scenario labels → earnings_v1 features → scenario classifier.

Writes readiness snapshot for analyzer (last_earnings_intelligence_readiness.json).

Examples:
  python scripts/run_earnings_ml_refresh.py --dry-run
  python scripts/run_earnings_ml_refresh.py --backfill-limit 200
  ML_READINESS_TRAIN_MODE=full python scripts/run_earnings_ml_refresh.py
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


def _default_q_dir() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    return project_root / "local" / "logs" / "ml_data_quality"


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings ML refresh pipeline")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--backfill-limit", type=int, default=600)
    ap.add_argument("--skip-labels", action="store_true")
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-readiness", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = mode in ("full", "train", "write", "prod")
    dry_run = args.dry_run or not full_train

    q_dir = _default_q_dir()
    q_dir.mkdir(parents=True, exist_ok=True)
    scenario_metrics_path = q_dir / "last_event_reaction_scenario_train_metrics.json"

    py = sys.executable
    if not args.skip_labels:
        label_cmd = [py, "scripts/apply_earnings_scenario_labels.py", "--dataset-version", args.dataset_version]
        if dry_run:
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
        if dry_run:
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
            "--json-metrics-out",
            str(scenario_metrics_path),
        ]
        if dry_run:
            train_cmd.append("--dry-run")
        rc = _run(train_cmd)
        if rc != 0 and not dry_run:
            logger.warning("Scenario classifier train returned %s (may be insufficient labels)", rc)

    if not args.skip_readiness:
        from report_generator import get_engine
        from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness

        write_earnings_intelligence_readiness(
            get_engine(),
            project_root=project_root,
            scenario_metrics_path=scenario_metrics_path,
        )

    if not args.skip_readiness and not dry_run:
        shadow_cmd = [py, "scripts/run_earnings_scenario_shadow_report.py"]
        rc = _run(shadow_cmd)
        if rc == 0:
            from report_generator import get_engine
            from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness

            write_earnings_intelligence_readiness(
                get_engine(),
                project_root=project_root,
                scenario_metrics_path=scenario_metrics_path,
            )

    logger.info("Earnings ML refresh completed (dry_run=%s)", dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

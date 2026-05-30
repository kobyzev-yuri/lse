#!/usr/bin/env python3
"""
Refresh earnings ML stack: labels → outcomes → features → train → readiness.

Operational loop (prod cron):
  - Every 6h: apply dataset updates + incremental retrain (EARNINGS_ML_REFRESH_APPLY_DATA=1)
  - Nightly 23:52: full train + shadow report (--full / ML_READINESS_TRAIN_MODE=full)

Examples:
  python scripts/run_earnings_ml_refresh.py --dry-run
  python scripts/run_earnings_ml_refresh.py --apply-data --incremental-train
  ML_READINESS_TRAIN_MODE=full python scripts/run_earnings_ml_refresh.py --full
"""
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

EARNINGS_FBV = "quotes_regime_earnings_v1"


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        from config_loader import get_config_value

        raw = (get_config_value(key, "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    logger.info("run: %s", " ".join(cmd))
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.call(cmd, env=full_env, cwd=str(project_root))


def _default_q_dir() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    return project_root / "local" / "logs" / "ml_data_quality"


def _write_readiness(
    *,
    project_root: Path,
    scenario_metrics_path: Path,
    peer_spillover_metrics_path: Path,
) -> None:
    from report_generator import get_engine
    from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness

    write_earnings_intelligence_readiness(
        get_engine(),
        project_root=project_root,
        scenario_metrics_path=scenario_metrics_path,
        peer_spillover_metrics_path=peer_spillover_metrics_path,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings ML refresh pipeline")
    ap.add_argument("--dry-run", action="store_true", help="No DB writes and no model .cbm (overrides apply/train flags)")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--backfill-limit", type=int, default=600)
    ap.add_argument("--outcomes-limit", type=int, default=800)
    ap.add_argument("--skip-labels", action="store_true")
    ap.add_argument("--skip-outcomes", action="store_true")
    ap.add_argument("--skip-backfill", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-readiness", action="store_true")
    ap.add_argument("--skip-shadow", action="store_true")
    ap.add_argument(
        "--apply-data",
        action="store_true",
        help="Write labels/outcomes/features to DB (also via EARNINGS_ML_REFRESH_APPLY_DATA=1)",
    )
    ap.add_argument(
        "--incremental-train",
        action="store_true",
        help="Write .cbm on non-full runs (also via EARNINGS_ML_REFRESH_INCREMENTAL_TRAIN=1)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Full pipeline: apply data + train + shadow + readiness",
    )
    args = ap.parse_args()

    from config_loader import get_config_value

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    apply_data = full_train or args.apply_data or _env_bool("EARNINGS_ML_REFRESH_APPLY_DATA", False)
    incremental_train = full_train or args.incremental_train or _env_bool("EARNINGS_ML_REFRESH_INCREMENTAL_TRAIN", False)
    train_dry_run = args.dry_run or not (full_train or incremental_train)
    data_dry_run = args.dry_run or not apply_data

    logger.info(
        "ML refresh mode=%s apply_data=%s train_dry_run=%s full=%s",
        mode if not args.full else "full(cli)",
        apply_data,
        train_dry_run,
        full_train,
    )

    q_dir = _default_q_dir()
    q_dir.mkdir(parents=True, exist_ok=True)
    scenario_metrics_path = q_dir / "last_event_reaction_scenario_train_metrics.json"
    peer_spillover_metrics_path = q_dir / "last_peer_spillover_train_metrics.json"
    peer_dataset_path = q_dir / "peer_spillover_dataset.json"

    py = sys.executable

    if not args.skip_labels:
        label_cmd = [
            py,
            "scripts/apply_earnings_scenario_labels.py",
            "--dataset-version",
            args.dataset_version,
            "--universe",
        ]
        if data_dry_run:
            label_cmd.append("--dry-run")
        rc = _run(label_cmd)
        if rc != 0:
            return rc

    if not args.skip_outcomes:
        outcomes_cmd = [
            py,
            "scripts/backfill_event_reaction_labeling.py",
            "--dataset-version",
            args.dataset_version,
            "--only-outcomes",
            "--include-earnings-universe",
            "--limit",
            str(max(1, args.outcomes_limit)),
        ]
        if full_train:
            outcomes_cmd.append("--force-outcomes")
        if data_dry_run:
            outcomes_cmd.append("--dry-run")
        rc = _run(outcomes_cmd)
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
            "--include-earnings-universe",
            "--limit",
            str(max(1, args.backfill_limit)),
        ]
        if data_dry_run:
            backfill_cmd.append("--dry-run")
        rc = _run(
            backfill_cmd,
            env={"EVENT_REACTION_FEATURE_BUILDER_VERSION": EARNINGS_FBV},
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
            EARNINGS_FBV,
            "--json-metrics-out",
            str(scenario_metrics_path),
        ]
        if train_dry_run:
            train_cmd.append("--dry-run")
        rc = _run(train_cmd)
        if rc != 0 and not train_dry_run:
            logger.warning("Scenario classifier train returned %s (may be insufficient labels)", rc)

        peer_dataset_cmd = [
            py,
            "scripts/build_peer_spillover_dataset.py",
            "--dataset-version",
            args.dataset_version,
            "--since",
            "2026-01-01",
        ]
        if train_dry_run:
            peer_dataset_cmd.append("--dry-run")
        else:
            peer_dataset_cmd.extend(["--json-out", str(peer_dataset_path)])
        rc = _run(peer_dataset_cmd)
        if rc != 0 and not train_dry_run:
            logger.warning("Peer spillover dataset build returned %s", rc)

        peer_train_cmd = [
            py,
            "scripts/train_peer_spillover_regressor.py",
            "--dataset-version",
            args.dataset_version,
            "--feature-builder-version",
            EARNINGS_FBV,
            "--json-metrics-out",
            str(peer_spillover_metrics_path),
        ]
        if train_dry_run:
            peer_train_cmd.append("--dry-run")
        rc = _run(peer_train_cmd)
        if rc != 0 and not train_dry_run:
            logger.warning("Peer spillover train returned %s (may be insufficient rows)", rc)

    if not args.skip_readiness:
        _write_readiness(
            project_root=project_root,
            scenario_metrics_path=scenario_metrics_path,
            peer_spillover_metrics_path=peer_spillover_metrics_path,
        )

    if full_train and not args.skip_shadow and not train_dry_run:
        shadow_cmd = [py, "scripts/run_earnings_scenario_shadow_report.py"]
        rc = _run(shadow_cmd)
        if rc == 0:
            _write_readiness(
                project_root=project_root,
                scenario_metrics_path=scenario_metrics_path,
                peer_spillover_metrics_path=peer_spillover_metrics_path,
            )

    logger.info(
        "Earnings ML refresh completed apply_data=%s train_dry_run=%s full=%s",
        apply_data,
        train_dry_run,
        full_train,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

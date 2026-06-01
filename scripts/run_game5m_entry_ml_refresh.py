#!/usr/bin/env python3
"""
GAME_5M entry CatBoost refresh: datasets (optional) → train → unified refresh log.

Data-driven trigger: new closed GAME_5M BUY rows since last apply/train.
See docs/ML_UNIFIED_RETRAIN_FRAMEWORK.md
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


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _run(cmd: list[str], *, cwd: Path) -> int:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(cwd))


def _default_q_dir() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    return project_root / "local" / "logs" / "ml_data_quality"


def main() -> int:
    ap = argparse.ArgumentParser(description="GAME_5M entry ML refresh")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--skip-datasets", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.ml_contour_runner import finalize_contour_refresh, plan_contour_refresh

    trigger, _gates, deltas = plan_contour_refresh(
        "game5m_entry",
        project_root,
        get_engine(),
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("GAME_5M entry refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root, "game5m_entry", trigger, apply_ran=False, train_ran=False, full=False, skipped=True
        )
        return 0

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    apply_data = args.apply_data or trigger.should_apply_data or full_train
    do_train = (trigger.should_train or full_train) and not args.skip_train
    train_dry_run = args.dry_run or not do_train or (not full_train and mode == "dry_run")
    data_dry_run = args.dry_run or not apply_data

    q_dir = _default_q_dir()
    q_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = q_dir / "last_game5m_train_metrics.json"
    py = sys.executable
    base_out = Path("/app/logs/ml") if Path("/app/logs").exists() else project_root / "local" / "logs" / "ml"
    if os.environ.get("DAILY_ML_OUT_DIR"):
        base_out = Path(os.environ["DAILY_ML_OUT_DIR"])
    datasets_dir = base_out / "datasets"
    models_dir = base_out / "models"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    datasets_ran = False
    if apply_data and not args.skip_datasets and not data_dry_run:
        ds_flags: list[str] = []
        if data_dry_run:
            ds_flags.append("--dry-run")
        stuck_out = datasets_dir / "game5m_stuck_dataset.csv"
        cont_out = datasets_dir / "game5m_continuation_dataset.csv"
        rc1 = _run(
            [py, str(project_root / "scripts/build_game5m_stuck_dataset.py"), "--out", str(stuck_out)] + ds_flags,
            cwd=project_root,
        )
        rc2 = _run(
            [py, str(project_root / "scripts/build_game5m_continuation_dataset.py"), "--out", str(cont_out)]
            + ds_flags,
            cwd=project_root,
        )
        datasets_ran = rc1 == 0 and rc2 == 0

    train_rc = 0
    if do_train:
        cb_out = models_dir / "game5m_entry_catboost.cbm"
        train_cmd = [
            py,
            str(project_root / "scripts/train_game5m_catboost.py"),
            "--out",
            str(cb_out),
            "--json-metrics-out",
            str(metrics_path),
        ]
        if train_dry_run:
            train_cmd.append("--dry-run")
        train_rc = _run(train_cmd, cwd=project_root)

    finalize_contour_refresh(
        project_root,
        "game5m_entry",
        trigger,
        apply_ran=apply_data and not data_dry_run and datasets_ran,
        train_ran=do_train and not train_dry_run,
        full=full_train,
        extra={"train_rc": train_rc, "datasets_ran": datasets_ran},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

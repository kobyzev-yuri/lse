#!/usr/bin/env python3
"""Portfolio CatBoost refresh with data-driven trigger (new closed PORTFOLIO round-trips)."""
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
    ap = argparse.ArgumentParser(description="Portfolio ML refresh")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.ml_contour_refresh import contour_continuous_enabled, get_contour_spec
    from services.ml_contour_runner import (
        finalize_contour_refresh,
        plan_contour_refresh,
        should_write_catboost_model,
    )

    spec = get_contour_spec("portfolio")
    engine = get_engine()
    trigger, gates, deltas = plan_contour_refresh(
        "portfolio",
        project_root,
        engine,
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("Portfolio refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root, "portfolio", trigger, apply_ran=False, train_ran=False, full=False, skipped=True
        )
        return 0

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    do_train = trigger.should_train or full_train
    continuous = contour_continuous_enabled(spec, product_ready=gates.get("product_ready", False))
    writes_cbm = should_write_catboost_model(
        cli_dry_run=args.dry_run,
        do_train=do_train,
        full_train=full_train,
        readiness_train_mode=mode,
        phase=trigger.phase,
        continuous_enabled=continuous,
    )
    train_dry_run = not writes_cbm

    q_dir = _default_q_dir()
    metrics_path = q_dir / "last_portfolio_train_metrics.json"
    metrics_20d_path = q_dir / "last_portfolio_20d_train_metrics.json"
    py = sys.executable
    train_cmd = [
        py,
        str(project_root / "scripts/train_portfolio_catboost.py"),
        "--json-metrics-out",
        str(metrics_path),
    ]
    if train_dry_run:
        train_cmd.append("--dry-run")
    logger.info(
        "Portfolio refresh train writes_cbm=%s continuous=%s phase=%s",
        writes_cbm,
        continuous,
        trigger.phase,
    )
    train_rc = subprocess.call(train_cmd, cwd=str(project_root)) if do_train else 0

    # 20d trend overlay: same write gate as 5d (or explicit PORTFOLIO_CATBOOST_20D_CONTINUOUS_TRAIN).
    train_20d = (get_config_value("PORTFOLIO_CATBOOST_20D_CONTINUOUS_TRAIN", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    train_rc_20d = 0
    if do_train and train_20d:
        out_20d = (get_config_value("PORTFOLIO_CATBOOST_20D_MODEL_PATH") or "").strip()
        if not out_20d:
            out_20d = (
                "/app/logs/ml/models/portfolio_return_catboost_20d.cbm"
                if Path("/app/logs").exists()
                else str(project_root / "local" / "models" / "portfolio_return_catboost_20d.cbm")
            )
        train_cmd_20d = [
            py,
            str(project_root / "scripts/train_portfolio_catboost.py"),
            "--horizon-days",
            "20",
            "--min-rows",
            "300",
            "--out",
            out_20d,
            "--json-metrics-out",
            str(metrics_20d_path),
        ]
        if train_dry_run:
            train_cmd_20d.append("--dry-run")
        logger.info(
            "Portfolio 20d refresh train writes_cbm=%s out=%s",
            writes_cbm,
            out_20d,
        )
        train_rc_20d = subprocess.call(train_cmd_20d, cwd=str(project_root))

    finalize_contour_refresh(
        project_root,
        "portfolio",
        trigger,
        apply_ran=True,
        train_ran=writes_cbm and train_rc == 0,
        full=full_train,
        extra={
            "train_rc": train_rc,
            "train_rc_20d": train_rc_20d,
            "train_20d": bool(train_20d and do_train),
            "writes_cbm_20d": bool(writes_cbm and train_20d and do_train and train_rc_20d == 0),
        },
    )
    if train_rc != 0:
        return train_rc
    if train_20d and do_train and train_rc_20d != 0:
        return train_rc_20d
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

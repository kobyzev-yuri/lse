#!/usr/bin/env python3
"""Gap forecast refresh: pool DB metrics (+ optional OLS coef suggestions) on trigger."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
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
    ap = argparse.ArgumentParser(description="Gap forecast ML refresh (metrics / refit advisory)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--days", type=int, default=0, help="Lookback days (0=config default)")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.ml_contour_runner import finalize_contour_refresh, plan_contour_refresh

    trigger, gates, deltas = plan_contour_refresh(
        "gap_forecast",
        project_root,
        get_engine(),
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("Gap forecast refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root, "gap_forecast", trigger, apply_ran=False, train_ran=False, full=False, skipped=True
        )
        return 0

    lookback = args.days or int((get_config_value("ML_GAP_FORECAST_ANALYZE_DAYS") or "90").strip() or "90")
    lookback = max(30, min(450, lookback))
    suggest = args.full or (get_config_value("ML_GAP_FORECAST_SUGGEST_COEFS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    q_dir = _default_q_dir()
    q_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = q_dir / "last_gap_forecast_metrics.json"

    if args.dry_run:
        finalize_contour_refresh(
            project_root,
            "gap_forecast",
            trigger,
            apply_ran=False,
            train_ran=False,
            full=args.full,
            skipped=False,
            extra={"dry_run": True},
        )
        return 0

    py = sys.executable
    cmd = [
        py,
        str(project_root / "scripts/analyze_game5m_gap_forecast.py"),
        "--days",
        str(lookback),
        "--json-metrics-out",
        str(metrics_path),
    ]
    if suggest:
        cmd.append("--suggest-coefs")

    import subprocess

    rc = subprocess.call(cmd, cwd=str(project_root))
    wrote = metrics_path.is_file()

    finalize_contour_refresh(
        project_root,
        "gap_forecast",
        trigger,
        apply_ran=wrote,
        train_ran=wrote and rc == 0,
        full=args.full,
        extra={"analyze_rc": rc, "lookback_days": lookback, "suggest_coefs": suggest},
    )
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    raise SystemExit(main())

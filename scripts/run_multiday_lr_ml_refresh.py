#!/usr/bin/env python3
"""Multiday LR ridge refresh: refit per-ticker JSON when data-driven trigger fires."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
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
    ap = argparse.ArgumentParser(description="Multiday LR ML refresh")
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

    spec = get_contour_spec("multiday_lr")
    trigger, gates, deltas = plan_contour_refresh(
        "multiday_lr",
        project_root,
        get_engine(),
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("Multiday LR refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root, "multiday_lr", trigger, apply_ran=False, train_ran=False, full=False, skipped=True
        )
        return 0

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    do_train = trigger.should_train or full_train
    continuous = contour_continuous_enabled(spec, product_ready=gates.get("product_ready", False))
    writes = should_write_catboost_model(
        cli_dry_run=args.dry_run,
        do_train=do_train,
        full_train=full_train,
        readiness_train_mode=mode,
        phase=trigger.phase,
        continuous_enabled=continuous,
    )
    train_dry_run = not writes

    q_dir = _default_q_dir()
    q_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = q_dir / "last_multiday_lr_train_metrics.json"
    tickers_source = (get_config_value("ML_MULTIDAY_LR_TICKERS_SOURCE") or "game5m").strip()

    py = sys.executable
    train_cmd = [
        py,
        str(project_root / "scripts/train_game5m_multiday_lr.py"),
        "--tickers-source",
        tickers_source,
        "--json-metrics-out",
        str(metrics_path),
    ]
    if train_dry_run:
        train_cmd.append("--dry-run")

    train_rc = subprocess.call(train_cmd, cwd=str(project_root)) if do_train else 0

    summary: dict = {"status": "skipped"}
    if metrics_path.is_file():
        try:
            raw = json.loads(metrics_path.read_text(encoding="utf-8"))
            rows = raw if isinstance(raw, list) else []
            summary = {
                "status": "ok" if rows else "empty",
                "n_tickers_fitted": len(rows),
                "tickers_source": tickers_source,
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            summary = {"status": "metrics_read_error", "error": str(e)}

    finalize_contour_refresh(
        project_root,
        "multiday_lr",
        trigger,
        apply_ran=True,
        train_ran=writes and train_rc == 0,
        full=full_train,
        extra={"train_rc": train_rc, "summary": summary},
    )
    return 0 if train_rc == 0 else train_rc


if __name__ == "__main__":
    raise SystemExit(main())

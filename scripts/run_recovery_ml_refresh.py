#!/usr/bin/env python3
"""Recovery CatBoost refresh: analyzer JSONL export → train when trigger fires."""
from __future__ import annotations

import argparse
import json
import logging
import os
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


def _resolve_recovery_jsonl(*, cli_path: str = "") -> str:
    """JSONL for train: --jsonl > env/config > newest game5m_recovery_ml_*.jsonl in /app/logs."""
    explicit = (cli_path or os.environ.get("GAME_5M_RECOVERY_TRAIN_JSONL") or "").strip()
    if not explicit:
        try:
            from config_loader import get_config_value

            explicit = (get_config_value("GAME_5M_RECOVERY_TRAIN_JSONL", "") or "").strip()
        except Exception:
            explicit = ""
    if explicit and Path(explicit).is_file():
        return explicit
    log_dirs = [Path("/app/logs"), project_root / "local" / "logs", project_root / "logs"]
    newest: tuple[float, str] = (0.0, "")
    for d in log_dirs:
        if not d.is_dir():
            continue
        for p in d.glob("game5m_recovery_ml_*.jsonl"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime >= newest[0]:
                newest = (mtime, str(p))
    return newest[1]


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        try:
            from config_loader import get_config_value

            raw = (get_config_value(key, "") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return default
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description="Recovery ML refresh")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--skip-export", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--jsonl", default="", help="Recovery export JSONL (skip-export or re-train)")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.ml_contour_refresh import contour_continuous_enabled, get_contour_spec
    from services.ml_contour_runner import (
        finalize_contour_refresh,
        plan_contour_refresh,
        should_write_catboost_model,
    )

    spec = get_contour_spec("recovery")
    trigger, gates, deltas = plan_contour_refresh(
        "recovery",
        project_root,
        get_engine(),
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("Recovery refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root, "recovery", trigger, apply_ran=False, train_ran=False, full=False, skipped=True
        )
        return 0

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    apply_data = args.apply_data or trigger.should_apply_data or full_train
    do_train = (trigger.should_train or full_train) and not args.skip_train
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
    metrics_path = q_dir / "last_recovery_train_metrics.json"
    days = max(7, min(60, _env_int("DAILY_RECOVERY_DAYS", 30)))
    horizon = max(15, min(780, _env_int("DAILY_RECOVERY_HORIZON_MINUTES", 120)))

    export_ok = False
    jsonl_path = ""
    lines = 0
    if apply_data and not args.skip_export and not args.dry_run:
        from services.trade_effectiveness_analyzer import analyze_trade_effectiveness

        logger.info("recovery export: days=%s", days)
        # Export-only sections: skip ml_arbiters (multiday/gap on full universe — hours of CPU).
        payload = analyze_trade_effectiveness(
            days=days,
            strategy="GAME_5M",
            export_recovery_ml=True,
            use_llm=False,
            sections="recovery",
        )
        exp = payload.get("game5m_hold_recovery_export") if isinstance(payload, dict) else None
        if isinstance(exp, dict) and exp.get("status") == "ok":
            export_ok = True
            jsonl_path = str(exp.get("path") or "")
            lines = int(exp.get("lines_written") or 0)
            logger.info("recovery export ok: %s lines=%s", jsonl_path, lines)
        else:
            logger.warning("recovery export failed: %s", exp)

    train_rc = 0
    if do_train and jsonl_path:
        py = sys.executable
        train_cmd = [
            py,
            str(project_root / "scripts/train_game5m_recovery_catboost.py"),
            "--jsonl",
            jsonl_path,
            "--horizon",
            str(horizon),
            "--json-metrics-out",
            str(metrics_path),
        ]
        if train_dry_run:
            train_cmd.append("--dry-run")
        train_rc = subprocess.call(train_cmd, cwd=str(project_root))
    elif do_train and not jsonl_path:
        jsonl_path = _resolve_recovery_jsonl(cli_path=args.jsonl)
        if jsonl_path:
            logger.info("recovery train using jsonl: %s", jsonl_path)
            py = sys.executable
            train_cmd = [
                py,
                str(project_root / "scripts/train_game5m_recovery_catboost.py"),
                "--jsonl",
                jsonl_path,
                "--horizon",
                str(horizon),
                "--json-metrics-out",
                str(metrics_path),
            ]
            if train_dry_run:
                train_cmd.append("--dry-run")
            train_rc = subprocess.call(train_cmd, cwd=str(project_root))
        else:
            logger.warning(
                "recovery train skipped: no export jsonl (use --jsonl PATH or export first)"
            )
            train_rc = 0

    finalize_contour_refresh(
        project_root,
        "recovery",
        trigger,
        apply_ran=export_ok,
        train_ran=writes and train_rc == 0,
        full=full_train,
        extra={
            "train_rc": train_rc,
            "export_lines": lines,
            "export_path": jsonl_path,
            "horizon_minutes": horizon,
        },
    )
    return 0 if train_rc == 0 else train_rc


if __name__ == "__main__":
    raise SystemExit(main())

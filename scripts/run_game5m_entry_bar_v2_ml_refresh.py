#!/usr/bin/env python3
"""
GAME_5M entry bar v2 refresh: bar-level dataset (triple barrier) → train shadow CatBoost v2.

Does not replace prod v1 (trade-based game5m_entry). See docs/GAME_5M_PREDICTOR_DATASET_PLAN.md
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


def _env_str(key: str, default: str) -> str:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        from config_loader import get_config_value

        raw = (get_config_value(key, "") or "").strip()
    return raw or default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env_str(key, "1" if default else "0").lower()
    return raw in ("1", "true", "yes")


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        from config_loader import get_config_value

        raw = (get_config_value(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _run(cmd: list[str], *, cwd: Path) -> int:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(cwd))


def _ml_base_out() -> Path:
    if os.environ.get("DAILY_ML_OUT_DIR"):
        return Path(os.environ["DAILY_ML_OUT_DIR"])
    if Path("/app/logs").exists():
        return Path("/app/logs/ml")
    return project_root / "local" / "logs" / "ml"


def main() -> int:
    ap = argparse.ArgumentParser(description="GAME_5M entry bar v2 ML refresh (shadow)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--skip-datasets", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.ml_contour_runner import finalize_contour_refresh, plan_contour_refresh

    contour_id = "game5m_entry_bar_v2"
    trigger, _gates, deltas = plan_contour_refresh(
        contour_id,
        project_root,
        get_engine(),
        force_full=args.full,
        force_apply=args.apply_data,
    )
    if not trigger.should_apply_data and not trigger.should_train and not args.dry_run:
        logger.info("entry bar v2 refresh skipped: %s %s", trigger.reasons, deltas)
        finalize_contour_refresh(
            project_root,
            contour_id,
            trigger,
            apply_ran=False,
            train_ran=False,
            full=False,
            skipped=True,
        )
        return 0

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    apply_data = args.apply_data or trigger.should_apply_data or full_train
    do_train = (trigger.should_train or full_train) and not args.skip_train
    train_dry_run = args.dry_run or not do_train or (not full_train and mode == "dry_run")
    data_dry_run = args.dry_run or not apply_data

    q_dir = Path("/app/logs/ml/ml_data_quality") if Path("/app/logs").exists() else project_root / "local" / "logs" / "ml_data_quality"
    q_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = q_dir / "last_game5m_entry_bar_v2_train_metrics.json"

    py = sys.executable
    base_out = _ml_base_out()
    datasets_dir = base_out / "datasets"
    models_dir = base_out / "models"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    csv_out = datasets_dir / "game5m_entry_bar_dataset.csv"
    stats_out = datasets_dir / "game5m_entry_bar_dataset_stats.json"
    train_json_out = datasets_dir / "game5m_entry_bar_v2_train.json"
    days = _env_int("GAME_5M_ENTRY_BAR_BUILD_DAYS", 90)
    train_population = _env_str("GAME_5M_ENTRY_BAR_V2_TRAIN_POPULATION", "buy_only").lower()
    if train_population not in ("all", "buy_only"):
        train_population = "buy_only"
    do_calibrate = _env_bool("GAME_5M_ENTRY_BAR_V2_CALIBRATE", True)

    datasets_ran = False
    if apply_data and not args.skip_datasets and not data_dry_run:
        build_cmd = [
            py,
            str(project_root / "scripts/build_game5m_entry_bar_dataset.py"),
            "--days",
            str(days),
            "--source",
            "db",
            "--out",
            str(csv_out),
            "--summary-json",
            str(stats_out),
        ]
        if data_dry_run:
            build_cmd.append("--dry-run")
        rc_build = _run(build_cmd, cwd=project_root)
        datasets_ran = rc_build == 0 and csv_out.is_file()

    train_rc = 0
    if do_train and csv_out.is_file():
        cb_out = models_dir / "game5m_entry_catboost_v2.cbm"
        train_cmd = [
            py,
            str(project_root / "scripts/train_game5m_catboost.py"),
            "--dataset",
            "bar",
            "--bar-csv",
            str(csv_out),
            "--out",
            str(cb_out),
            "--train-population",
            train_population,
            "--json-metrics-out",
            str(metrics_path),
        ]
        if do_calibrate:
            train_cmd.append("--calibrate")
        alt_metrics = train_json_out
        if train_dry_run:
            train_cmd.append("--dry-run")
        train_rc = _run(train_cmd, cwd=project_root)
        if metrics_path.is_file() and not train_dry_run:
            try:
                train_json_out.write_text(metrics_path.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception as e:
                logger.warning("copy train metrics to %s: %s", train_json_out, e)
    elif do_train and not csv_out.is_file():
        logger.warning("skip train: dataset CSV missing at %s", csv_out)
        train_rc = 2

    if not train_dry_run and metrics_path.is_file():
        try:
            from services.unified_trust_arbiter import write_unified_trust_arbiter

            write_unified_trust_arbiter(project_root=project_root, report=None)
        except Exception as e:
            logger.warning("trust arbiter refresh after bar v2 train: %s", e)

    finalize_contour_refresh(
        project_root,
        contour_id,
        trigger,
        apply_ran=apply_data and not data_dry_run and datasets_ran,
        train_ran=do_train and not train_dry_run and train_rc == 0,
        full=full_train,
        extra={
            "train_rc": train_rc,
            "datasets_ran": datasets_ran,
            "dataset_csv": str(csv_out),
            "metrics_path": str(metrics_path),
        },
    )
    return 0 if train_rc in (0, 2) else train_rc


if __name__ == "__main__":
    raise SystemExit(main())

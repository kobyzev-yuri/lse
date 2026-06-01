#!/usr/bin/env python3
"""
Refresh open-path ML stack: labels → dataset → train → shadow → readiness.

Operational loop (prod cron):
  - Nightly: label rows with completed sessions
  - Every 6h + nightly: incremental train (continuous learning on fresh sessions)
  - Sun/full: train + shadow + readiness (--full / ML_READINESS_OPEN_PATH_TRAIN_MODE=full)
  - After product_ready: OPEN_PATH_ML_CONTINUOUS_TRAIN=1 keeps retraining on all history

Examples:
  python scripts/run_open_path_ml_refresh.py --dry-run
  python scripts/run_open_path_ml_refresh.py --apply-data --incremental-train
  ML_READINESS_OPEN_PATH_TRAIN_MODE=full python scripts/run_open_path_ml_refresh.py --full
"""
from __future__ import annotations

import argparse
import json
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
    if not raw:
        from config_loader import get_config_value

        raw = (get_config_value(key, "") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _product_ready_from_disk() -> bool:
    try:
        from services.open_path_readiness import default_readiness_metrics_path, _json_load

        bundle = _json_load(default_readiness_metrics_path(project_root))
        return bool((bundle or {}).get("gates", {}).get("overall_open_path_classifier_ready"))
    except Exception:
        return False


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


def _write_readiness(*, open_path_metrics_path: Path, open_path_dataset_path: Path) -> None:
    from report_generator import get_engine
    from services.open_path_readiness import write_open_path_readiness

    write_open_path_readiness(
        get_engine(),
        project_root=project_root,
        train_metrics_path=open_path_metrics_path,
        dataset_path=open_path_dataset_path,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Open-path ML refresh pipeline")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--skip-labels", action="store_true")
    ap.add_argument("--skip-dataset", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-shadow", action="store_true")
    ap.add_argument("--skip-readiness", action="store_true")
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--incremental-train", action="store_true")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value

    product_ready = _product_ready_from_disk()
    continuous_train = _env_bool("OPEN_PATH_ML_CONTINUOUS_TRAIN", product_ready)

    mode = (get_config_value("ML_READINESS_OPEN_PATH_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = args.full or mode in ("full", "train", "write", "prod")
    apply_data = full_train or args.apply_data or _env_bool("OPEN_PATH_ML_REFRESH_APPLY_DATA", False)
    incremental_train = (
        full_train
        or args.incremental_train
        or _env_bool("OPEN_PATH_ML_REFRESH_INCREMENTAL_TRAIN", False)
        or (continuous_train and apply_data)
    )
    train_dry_run = args.dry_run or not (full_train or incremental_train)
    data_dry_run = args.dry_run or not apply_data

    logger.info(
        "Open-path ML refresh mode=%s apply_data=%s train_dry_run=%s full=%s continuous=%s product_ready=%s",
        mode if not args.full else "full(cli)",
        apply_data,
        train_dry_run,
        full_train,
        continuous_train,
        product_ready,
    )

    q_dir = _default_q_dir()
    q_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = q_dir / "last_open_path_scenario_train_metrics.json"
    dataset_path = q_dir / "open_path_dataset.json"
    py = sys.executable
    labeled_ok = None
    n_trainable_after = None

    if not args.skip_labels:
        label_cmd = [py, "scripts/label_open_path_scenarios.py", "--ensure-table", "--since", args.since]
        if data_dry_run:
            label_cmd.append("--dry-run")
        rc = _run(label_cmd)
        if rc != 0:
            return rc

    if not args.skip_dataset:
        ds_cmd = [py, "scripts/build_open_path_dataset.py", "--since", args.since]
        if data_dry_run:
            ds_cmd.append("--dry-run")
        else:
            ds_cmd.extend(["--json-out", str(dataset_path)])
        rc = _run(ds_cmd)
        if rc != 0 and not data_dry_run:
            logger.warning("Open-path dataset build returned %s", rc)
        if dataset_path.is_file() and not data_dry_run:
            try:
                ds = json.loads(dataset_path.read_text(encoding="utf-8"))
                n_trainable_after = (ds.get("summary") or {}).get("n_trainable")
            except Exception:
                pass

    train_rc = 0
    if not args.skip_train:
        train_cmd = [
            py,
            "scripts/train_open_path_scenario_classifier.py",
            "--since",
            args.since,
            "--json-metrics-out",
            str(metrics_path),
        ]
        if train_dry_run:
            train_cmd.append("--dry-run")
        train_rc = _run(train_cmd)
        if train_rc != 0 and not train_dry_run:
            logger.warning("Open-path classifier train returned %s (may be insufficient rows)", train_rc)

    if not args.skip_readiness and not data_dry_run:
        _write_readiness(open_path_metrics_path=metrics_path, open_path_dataset_path=dataset_path)

    shadow_rc = 0
    if full_train and not args.skip_shadow and not train_dry_run:
        shadow_cmd = [py, "scripts/run_open_path_scenario_shadow_report.py", "--since", args.since]
        shadow_rc = _run(shadow_cmd)
        if shadow_rc == 0 and not args.skip_readiness:
            _write_readiness(open_path_metrics_path=metrics_path, open_path_dataset_path=dataset_path)

    from services.open_path_product_eta import write_refresh_log

    write_refresh_log(
        project_root=project_root,
        payload={
            "apply_data": apply_data,
            "train_ran": not train_dry_run and not args.skip_train,
            "train_rc": train_rc,
            "full": full_train,
            "continuous_train": continuous_train,
            "product_ready": product_ready,
            "labeled_ok": labeled_ok,
            "n_trainable_after": n_trainable_after,
            "shadow_ran": full_train and not train_dry_run and not args.skip_shadow,
            "shadow_rc": shadow_rc,
        },
    )

    logger.info(
        "Open-path ML refresh completed apply_data=%s train_dry_run=%s full=%s",
        apply_data,
        train_dry_run,
        full_train,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

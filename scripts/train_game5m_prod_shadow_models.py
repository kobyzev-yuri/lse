#!/usr/bin/env python3
"""Train prod shadow CatBoost models (E3 entry / H3 hold) aligned with ablation runner."""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Train E3/H3 shadow models for decision_stack")
    ap.add_argument("--entry-csv", default="local/datasets/game5m_entry_bar_full.csv")
    ap.add_argument("--hold-csv", default="local/datasets/game5m_hold_bar_dataset.csv")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    py = str(args.python)
    cmds = [
        [
            py,
            str(project_root / "scripts/train_game5m_catboost.py"),
            "--dataset",
            "bar",
            "--bar-csv",
            str(args.entry_csv),
            "--feature-mode",
            "full",
            "--out",
            str(project_root / "local/models/game5m_entry_catboost_e3.cbm"),
            "--json-metrics-out",
            str(project_root / "local/datasets/game5m_entry_catboost_e3_metrics.json"),
        ],
        [
            py,
            str(project_root / "scripts/train_game5m_hold_bar_catboost.py"),
            "--csv",
            str(args.hold_csv),
            "--feature-mode",
            "full",
            "--out",
            str(project_root / "local/models/game5m_hold_bar_catboost_h3.cbm"),
            "--json-metrics-out",
            str(project_root / "local/datasets/game5m_hold_bar_catboost_h3_metrics.json"),
        ],
    ]
    rc = 0
    for cmd in cmds:
        logger.info("run: %s", " ".join(cmd))
        if subprocess.call(cmd, cwd=str(project_root)) != 0:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

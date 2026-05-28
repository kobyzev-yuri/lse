#!/usr/bin/env python3
"""Run earnings scenario shadow report and write JSON for analyzer / UI."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings scenario shadow report")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.earnings_scenario_shadow import (
        default_shadow_report_path,
        write_earnings_scenario_shadow_report,
    )

    out_path = Path(args.json_out) if args.json_out.strip() else default_shadow_report_path(project_root)
    report = write_earnings_scenario_shadow_report(
        get_engine(),
        project_root=project_root,
        dataset_version=args.dataset_version,
        since=args.since,
    )
    if args.json_out.strip():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    agg = report.get("aggregate") or {}
    gate = report.get("trading_gate") or {}
    logger.info(
        "Shadow: n_matured=%s sign_acc=%s class_acc=%s mean_pnl_log=%s trading_ready=%s → %s",
        agg.get("n_matured"),
        agg.get("sign_accuracy"),
        agg.get("class_accuracy"),
        agg.get("mean_pseudo_pnl_log"),
        gate.get("ready"),
        out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

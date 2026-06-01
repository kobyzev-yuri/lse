#!/usr/bin/env python3
"""
Export open-path training rows summary (labels + features) for analyzer / debug.

Examples:
  python scripts/build_open_path_dataset.py --dry-run
  python scripts/build_open_path_dataset.py --json-out logs/open_path_dataset.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build open-path classifier dataset summary")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.open_path_classifier_dataset import (
        collect_open_path_classifier_coverage,
        load_open_path_training_frame,
        summarize_open_path_rows,
    )

    engine = get_engine()
    coverage = collect_open_path_classifier_coverage(engine, since=args.since.strip())
    frame = load_open_path_training_frame(engine, since=args.since.strip())
    rows = frame.to_dict(orient="records") if not frame.empty else []
    summary = {
        "coverage": coverage,
        "train_summary": summarize_open_path_rows(
            [{"scenario_label": r.get("target_scenario"), "symbol": r.get("symbol"), "trade_date": r.get("trade_date")} for r in rows]
        ),
        "n_trainable": len(rows),
    }
    logger.info("Open-path dataset: n_trainable=%s classes=%s", len(rows), coverage.get("labels_by_class"))

    if args.dry_run:
        for r in rows[:5]:
            logger.info("  %s %s → %s", r.get("trade_date"), r.get("symbol"), r.get("target_scenario"))
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0

    out_path = args.json_out.strip()
    if not out_path:
        out_dir = Path("/app/logs/ml/ml_data_quality") if Path("/app/logs").exists() else project_root / "local/logs/ml_data_quality"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "open_path_dataset.json")

    payload = {"summary": summary, "rows": rows[:5000]}
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %s (%s rows in export cap)", out_path, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train pooled premarket -> RTH open-gap model artifact."""
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
    p = argparse.ArgumentParser(description="Train pooled premarket gap ridge model.")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--min-rows", type=int, default=60)
    p.add_argument("--l2", type=float, default=5.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=str, default="", help="Override artifact path.")
    args = p.parse_args()

    from report_generator import get_engine
    from services.premarket_gap_model import (
        fetch_gap_training_rows,
        fit_pooled_gap_model,
        save_pooled_gap_artifact,
    )

    engine = get_engine()
    rows = fetch_gap_training_rows(engine, days=args.days)
    artifact = fit_pooled_gap_model(rows, min_rows=args.min_rows, l2=args.l2)
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    if artifact.get("ready") and not args.dry_run:
        path = save_pooled_gap_artifact(artifact, Path(args.out) if args.out else None)
        logger.info("Saved pooled gap model: %s", path)
    elif not artifact.get("ready"):
        logger.warning("Model not ready: %s", artifact.get("reason"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

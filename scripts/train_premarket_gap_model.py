#!/usr/bin/env python3
"""Train pooled premarket -> RTH open-gap ridge artifact."""
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
    p = argparse.ArgumentParser(description="Train pooled premarket gap ridge model.")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--min-rows", type=int, default=0, help="0 = config default")
    p.add_argument("--l2", type=float, default=0.0, help="0 = config default")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=str, default="", help="Override artifact path.")
    args = p.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine
    from services.premarket_gap_model import (
        _cfg_float,
        _cfg_int,
        fetch_gap_training_rows,
        fit_pooled_gap_model,
        premarket_gap_model_path,
        save_pooled_gap_artifact,
    )

    min_rows = args.min_rows or _cfg_int("GAME_5M_PREMARKET_GAP_MODEL_MIN_ROWS", 60)
    l2 = args.l2 if args.l2 > 0 else _cfg_float("GAME_5M_PREMARKET_GAP_MODEL_L2", 5.0)
    days = max(30, int(args.days))

    engine = get_engine()
    rows = fetch_gap_training_rows(engine, days=days)
    artifact = fit_pooled_gap_model(rows, min_rows=min_rows, l2=l2)
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    if artifact.get("ready") and not args.dry_run:
        out_path = Path(args.out) if args.out else None
        path = save_pooled_gap_artifact(artifact, out_path)
        logger.info("Saved pooled gap model: %s", path)
        return 0
    if not artifact.get("ready"):
        logger.warning("Model not ready: %s", artifact.get("reason"))
        return 1
    logger.info("Dry-run; artifact path would be %s", premarket_gap_model_path())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

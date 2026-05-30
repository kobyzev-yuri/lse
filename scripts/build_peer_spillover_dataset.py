#!/usr/bin/env python3
"""
Build peer spillover ML dataset rows: (source_event, peer) -> peer_forward_log_ret_5d.

Examples:
  python scripts/build_peer_spillover_dataset.py --dry-run
  python scripts/build_peer_spillover_dataset.py --json-out logs/peer_spillover_dataset.json
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
    ap = argparse.ArgumentParser(description="Build peer spillover dataset for ML Phase C")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--dry-run", action="store_true", help="Summary only")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.peer_spillover_dataset import build_peer_spillover_dataset_rows, summarize_peer_spillover_rows

    engine = get_engine()
    rows = build_peer_spillover_dataset_rows(
        engine,
        dataset_version=args.dataset_version.strip(),
        since=args.since.strip(),
        limit=args.limit,
    )
    summary = summarize_peer_spillover_rows(rows)
    logger.info("Peer spillover dataset: %s", summary)

    if args.dry_run:
        for r in rows[:5]:
            logger.info(
                "  %s %s -> %s peer_5d=%s weight=%s",
                r["source_symbol"],
                r["event_date"],
                r["peer_ticker"],
                r["peer_forward_log_ret_5d"],
                r["edge_weight"],
            )
        return 0

    out_path = args.json_out.strip()
    if not out_path:
        out_dir = project_root / "logs"
        if Path("/app/logs").exists():
            out_dir = Path("/app/logs")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "peer_spillover_dataset.json")

    payload = {"summary": summary, "rows": rows}
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s (%s rows)", out_path, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Weekly GAME_5M tactic scorecard — bundles, counterfactuals, experiment observe."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.weekly_game5m_tactic_review import (  # noqa: E402
    build_weekly_game5m_tactic_review,
    default_weekly_review_path,
    write_weekly_review,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly GAME_5M tactic review (bundle scorecard).")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--strategy", default="GAME_5M")
    ap.add_argument("--ledger", default="", help="Ledger path (default GAME5M_TUNING_LEDGER)")
    ap.add_argument("--limit-hold-to-gap", type=int, default=30)
    ap.add_argument(
        "--json-out",
        default=str(default_weekly_review_path()),
        help="Output JSON path (default ml_data_quality artifact)",
    )
    ap.add_argument("--stdout-only", action="store_true", help="Do not write artifact file")
    args = ap.parse_args()

    report = build_weekly_game5m_tactic_review(
        days=max(1, min(args.days, 30)),
        strategy=args.strategy,
        ledger_raw=args.ledger,
        limit_hold_to_gap=max(5, args.limit_hold_to_gap),
    )
    report["script"] = "scripts/weekly_game5m_tactic_review.py"
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if not args.stdout_only and args.json_out:
        out_path = write_weekly_review(report, path=args.json_out)
        print(f"\nWrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

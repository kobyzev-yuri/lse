#!/usr/bin/env python3
"""Print ML contour product status (dual-track: legacy vs decision_stack)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.ml_product_runtime import build_dual_track_summary  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="ML product runtime status (legacy + stack).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    summary = build_dual_track_summary()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"Executor: {summary['executor']}")
    print(f"RESOLVE: {summary['decision_stack_resolve']}  OWN_FINALIZE: {summary['decision_stack_own_finalize']}")
    print(f"Legacy field: {summary['legacy_hot_path_field']}")
    print(f"Promoted on legacy: {', '.join(summary['promoted_on_legacy']) or '(none)'}")
    print()
    for r in summary["contours"]:
        leg = "EXEC" if r["legacy_executes"] else "—"
        print(
            f"{r['contour_id']:28} tier={r['product_tier']:14} legacy={leg:4} stack={r['stack_role']:7}  {r['legacy_detail']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Mirror sign-off report for DECISION_STACK_RESOLVE (phase 3 consolidation).

Counts resolve_divergence in stored decision_snapshot on closed trades.
Use before enabling DECISION_STACK_RESOLVE_ENABLED=true.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.trade_effectiveness_analyzer import (  # noqa: E402
    _build_decision_stack_shadow_diff,
    _estimate_trade_effects,
    _load_closed_trades,
)


def _build_report(*, strategy: str, days: int, limit: int) -> dict:
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    effects = _estimate_trade_effects(closed, {})
    shadow = _build_decision_stack_shadow_diff(strategy, closed, effects, limit=limit)
    with_snap = int(shadow.get("trades_with_snapshot") or 0)
    diverged = int(shadow.get("divergence_count") or 0)
    rate = (diverged / with_snap) if with_snap else None
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "days": days,
        "closed_trades": len(closed),
        "trades_with_snapshot": with_snap,
        "divergence_count": diverged,
        "divergence_rate": round(rate, 4) if rate is not None else None,
        "resolve_enabled_recommendation": diverged == 0 and with_snap >= 5,
        "by_game": shadow.get("by_game") or {},
        "recent_divergences": shadow.get("recent_divergences") or [],
        "description": shadow.get("description"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Decision stack mirror divergence report.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--strategy", default="GAME_5M", help="GAME_5M | PORTFOLIO | ALL")
    ap.add_argument("--limit", type=int, default=20, help="Max recent divergence rows")
    ap.add_argument(
        "--max-divergences",
        type=int,
        default=0,
        help="Exit 1 if divergence_count exceeds this (cron gate)",
    )
    ap.add_argument("--json-out", default="", help="Optional path to write JSON")
    args = ap.parse_args()

    report = _build_report(strategy=args.strategy, days=args.days, limit=args.limit)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")

    if int(report.get("divergence_count") or 0) > max(0, args.max_divergences):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

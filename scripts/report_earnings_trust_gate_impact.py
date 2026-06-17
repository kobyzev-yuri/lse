#!/usr/bin/env python3
"""Отчёт влияния earnings_trust gate на projected resolve (shadow/live)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.decision_stack.earnings_trust_monitor import build_earnings_trust_gate_monitor
from services.trade_effectiveness_analyzer import _estimate_trade_effects, _load_closed_trades


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings trust gate impact report.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--json-out", default="", help="Optional path to write JSON")
    args = ap.parse_args()

    closed = _load_closed_trades(days=max(1, args.days), strategy_name="GAME_5M")
    effects = _estimate_trade_effects(closed, {})
    report = build_earnings_trust_gate_monitor(closed, limit=args.limit)
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["days_scanned"] = args.days
    report["closed_trades_scanned"] = len(closed)
    report["trade_effects_count"] = len(effects)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

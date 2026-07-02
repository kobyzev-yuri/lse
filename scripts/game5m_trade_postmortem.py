#!/usr/bin/env python3
"""CLI wrapper for services.game5m_trade_postmortem (cron + manual)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from services.game5m_trade_postmortem import (
    build_session_postmortem,
    format_session_markdown,
    last_session_snapshot_path,
    refresh_game5m_trade_postmortem,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GAME_5M trade post-mortem pipeline")
    parser.add_argument("--session-date", type=str, default=None, help="YYYY-MM-DD MSK")
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--no-write", action="store_true", help="stdout only, no JSONL/ledger")
    parser.add_argument("--no-ledger", action="store_true", help="skip tuning ledger sync")
    args = parser.parse_args(argv)

    session_day = date.fromisoformat(args.session_date) if args.session_date else datetime.now().date()

    if args.no_write:
        report = build_session_postmortem(session_day)
        print(format_session_markdown(report))
        return 0

    result = refresh_game5m_trade_postmortem(
        session_day,
        window_days=max(1, min(int(args.window_days), 90)),
        sync_ledger=not args.no_ledger,
    )
    print(format_session_markdown(result["session"]))
    print(f"\nTactics focus: {result['tactics'].get('training_focus')}", file=sys.stderr)
    for rec in result["tactics"].get("tactic_recommendations") or []:
        print(f"  - [{rec.get('priority')}] {rec.get('rationale_ru')}", file=sys.stderr)
    print(json.dumps(result["paths"], ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

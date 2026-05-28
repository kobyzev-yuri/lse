#!/usr/bin/env python3
"""
Build Earnings Event Brief JSON for UI/bot.

Examples:
  python scripts/build_earnings_event_brief.py --symbol META --event-date 2026-04-29
  python scripts/build_earnings_event_brief.py --symbol NVDA --event-date 2026-05-20 --json-out logs/earnings_materials/brief_NVDA.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_event_brief import build_event_brief  # noqa: E402


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Earnings Event Brief JSON")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--event-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    brief = build_event_brief(
        get_engine(),
        symbol=args.symbol,
        event_date=_parse_date(args.event_date),
        dataset_version=args.dataset_version.strip(),
    )
    text = json.dumps(brief, ensure_ascii=False, indent=2)
    if args.json_out.strip():
        out = Path(args.json_out.strip())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)
    return 0 if brief.get("status") in ("ok", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())

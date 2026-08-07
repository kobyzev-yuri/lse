#!/usr/bin/env python3
"""Manual Google Sheet SA news → knowledge_base sync.

Examples:
  python scripts/run_sa_sheet_ingest.py
  python scripts/run_sa_sheet_ingest.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest Seeking Alpha news from Google Sheet → KB")
    p.add_argument("--sheet-id", default="", help="Override NOTEBOOK_SA_SHEET_ID")
    p.add_argument("--credentials", default="", help="Override credentials JSON path")
    p.add_argument("--json", action="store_true", help="Print result as JSON")
    args = p.parse_args(argv)

    from services.sa_sheet_feed import credentials_path, ingest_sheet_to_kb, sheet_enabled, sheet_id

    cred = Path(args.credentials).expanduser() if args.credentials.strip() else None
    try:
        result = ingest_sheet_to_kb(
            spreadsheet_id=(args.sheet_id.strip() or None),
            credentials_file=cred,
        )
    except Exception as e:
        err = {"ok": False, "error": str(e), "sheet_id": args.sheet_id or sheet_id()}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    result = dict(result)
    result["ok"] = True
    result["enabled"] = sheet_enabled()
    result["credentials"] = str(cred or credentials_path())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        if result.get("skipped"):
            print(f"skipped: {result.get('reason')}")
        else:
            print(
                f"sheet_id={result.get('sheet_id')} rows={result.get('rows')} "
                f"kb_items={result.get('items')} kb_inserted={result.get('kb_inserted')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

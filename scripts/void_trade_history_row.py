#!/usr/bin/env python3
"""
Отмена ошибочной записи trade_history: бэкап в JSONL + DELETE.

Использование:
  python scripts/void_trade_history_row.py --trade-id 713 --reason timezone_age_bug --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text

from config_loader import get_database_url


def main() -> int:
    ap = argparse.ArgumentParser(description="Void (delete) a trade_history row with audit backup.")
    ap.add_argument("--trade-id", type=int, required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument(
        "--backup",
        default=str(project_root / "logs" / "voided_trades.jsonl"),
        help="Append-only audit log path",
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, ts, ticker, side, quantity, price, commission, signal_type,
                       total_value, strategy_name, ts_timezone, context_json
                FROM trade_history WHERE id = :id
            """),
            {"id": args.trade_id},
        ).mappings().first()
        if not row:
            print(f"id={args.trade_id}: not found")
            return 1

        record = dict(row)
        if record.get("context_json") is not None and not isinstance(record["context_json"], dict):
            record["context_json"] = json.loads(record["context_json"])

        audit = {
            "voided_at_utc": datetime.now(timezone.utc).isoformat(),
            "reason": args.reason,
            "note": args.note,
            "trade": {
                **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in record.items()},
            },
        }
        print(json.dumps(audit, ensure_ascii=False, indent=2)[:2000])

        if not args.apply:
            print("dry-run (use --apply to DELETE)")
            return 0

        backup_path = Path(args.backup)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(audit, ensure_ascii=False) + "\n")

        conn.execute(text("DELETE FROM trade_history WHERE id = :id"), {"id": args.trade_id})
        conn.commit()
        print(f"DELETED id={args.trade_id}, backup appended to {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

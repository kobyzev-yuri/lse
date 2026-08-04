#!/usr/bin/env python3
"""Subscribe to tipsters SA section feeds and take raw snapshots (~40/group).

Examples:
  python scripts/run_sa_section_snapshot.py --subscribe articles.market-outlook
  python scripts/run_sa_section_snapshot.py --snapshot --limit 40
  python scripts/run_sa_section_snapshot.py --snapshot-all-known --limit 20
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
    p = argparse.ArgumentParser(description="SA tipsters section subscriptions / snapshots")
    p.add_argument("--subscribe", action="append", default=[], help="Enable section id (repeatable)")
    p.add_argument("--unsubscribe", action="append", default=[], help="Disable section id (repeatable)")
    p.add_argument("--list", action="store_true", help="Print catalog + subscriptions")
    p.add_argument("--snapshot", action="store_true", help="Snapshot enabled subscriptions")
    p.add_argument("--snapshot-all-known", action="store_true", help="Snapshot all available catalog sections")
    p.add_argument("--limit", type=int, default=0, help="Per-group item limit (0 = from subs / config)")
    p.add_argument("--set-limit", type=int, default=0, help="Persist per_group_limit in subscriptions JSON")
    p.add_argument("--json", action="store_true", help="Print full JSON result")
    args = p.parse_args(argv)

    from services.sa_section_subscriptions import (
        catalog_with_subscriptions,
        run_section_snapshot,
        set_subscription,
        load_subscriptions,
        save_subscriptions,
    )

    if args.set_limit and args.set_limit > 0:
        doc = load_subscriptions()
        doc["per_group_limit"] = int(args.set_limit)
        save_subscriptions(doc)
        print(f"per_group_limit={doc['per_group_limit']}")

    for sid in args.subscribe:
        try:
            set_subscription(sid, True)
            print(f"subscribed {sid}")
        except Exception as e:
            print(f"subscribe {sid} failed: {e}", file=sys.stderr)
            return 1

    for sid in args.unsubscribe:
        try:
            set_subscription(sid, False)
            print(f"unsubscribed {sid}")
        except Exception as e:
            print(f"unsubscribe {sid} failed: {e}", file=sys.stderr)
            return 1

    if args.list or (
        not args.subscribe
        and not args.unsubscribe
        and not args.snapshot
        and not args.snapshot_all_known
        and not args.set_limit
    ):
        pack = catalog_with_subscriptions()
        if args.json:
            print(json.dumps(pack, ensure_ascii=False, indent=2))
        else:
            print(f"per_group_limit={pack.get('per_group_limit')} api_key={pack.get('has_api_key')}")
            for row in pack.get("catalog") or []:
                mark = "ON" if row.get("subscribed") else ("--" if row.get("available") else "NA")
                print(f"  [{mark}] {row.get('id')}: {row.get('title')} · {row.get('note')}")
            latest = pack.get("latest_snapshot")
            if latest:
                print(
                    f"latest: {latest.get('id')} · {latest.get('generated_at_utc')} · "
                    f"sections={latest.get('section_count')} items={latest.get('item_count')}"
                )
        if not (args.snapshot or args.snapshot_all_known):
            return 0

    lim = int(args.limit) if int(args.limit or 0) > 0 else None
    result = run_section_snapshot(
        all_available=bool(args.snapshot_all_known),
        limit=lim,
        write=True,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"snapshot id={result.get('archive_id') or result.get('id')} "
            f"sections={len(result.get('groups') or {})} items={result.get('item_count')}"
        )
        for sid, g in (result.get("groups") or {}).items():
            print(f"  {sid}: status={g.get('status')} count={g.get('count')} err={g.get('error') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

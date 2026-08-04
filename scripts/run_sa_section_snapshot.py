#!/usr/bin/env python3
"""SA tipsters subscriptions: sections + ticker mute → knowledge_base.

Examples:
  python scripts/run_sa_section_snapshot.py --list
  python scripts/run_sa_section_snapshot.py --subscribe articles.market-outlook
  python scripts/run_sa_section_snapshot.py --subscribe-all-available --ingest
  python scripts/run_sa_section_snapshot.py --ingest --limit 40
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
    p = argparse.ArgumentParser(description="SA tipsters section/ticker subscriptions → KB")
    p.add_argument("--subscribe", action="append", default=[], help="Enable section id (repeatable)")
    p.add_argument("--unsubscribe", action="append", default=[], help="Disable section id (repeatable)")
    p.add_argument("--subscribe-all-available", action="store_true", help="Enable all available sections")
    p.add_argument("--mute-ticker", action="append", default=[], help="Mute SA-fetch ticker (repeatable)")
    p.add_argument("--unmute-ticker", action="append", default=[], help="Enable SA-fetch ticker (repeatable)")
    p.add_argument("--list", action="store_true", help="Print catalog + subscriptions")
    p.add_argument("--ingest", action="store_true", help="Fetch enabled tickers+sections → KB")
    p.add_argument("--ingest-sections-only", action="store_true")
    p.add_argument("--ingest-tickers-only", action="store_true")
    p.add_argument("--all-sections", action="store_true", help="Ingest all available sections (ignore checkboxes)")
    p.add_argument("--limit", type=int, default=0, help="Per-group/ticker limit (0 = config)")
    p.add_argument("--set-limit", type=int, default=0, help="Persist per_group_limit")
    p.add_argument("--feed", action="store_true", help="Print KB SA feed summary")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    from services.sa_section_subscriptions import (
        catalog_with_subscriptions,
        load_sa_feed,
        load_subscriptions,
        run_sa_ingest,
        save_subscriptions,
        set_subscription,
        set_subscriptions_bulk,
        subscribe_all_available,
    )

    if args.set_limit and args.set_limit > 0:
        doc = load_subscriptions()
        doc["per_group_limit"] = int(args.set_limit)
        save_subscriptions(doc)
        print(f"per_group_limit={doc['per_group_limit']}")

    if args.subscribe_all_available:
        subscribe_all_available()
        print("subscribed all available sections")

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

    tick_map = {}
    for t in args.mute_ticker:
        tick_map[str(t).upper()] = False
    for t in args.unmute_ticker:
        tick_map[str(t).upper()] = True
    if tick_map:
        set_subscriptions_bulk(tickers=tick_map)
        print(f"tickers updated: {tick_map}")

    do_list = args.list or not (
        args.subscribe
        or args.unsubscribe
        or args.subscribe_all_available
        or args.mute_ticker
        or args.unmute_ticker
        or args.ingest
        or args.ingest_sections_only
        or args.ingest_tickers_only
        or args.set_limit
        or args.feed
    )

    if do_list:
        pack = catalog_with_subscriptions()
        if args.json:
            print(json.dumps(pack, ensure_ascii=False, indent=2))
        else:
            print(f"per_group_limit={pack.get('per_group_limit')} api_key={pack.get('has_api_key')}")
            for row in pack.get("catalog") or []:
                mark = "ON" if row.get("subscribed") else ("--" if row.get("available") else "NA")
                print(f"  [{mark}] {row.get('id')} → {row.get('kb_symbol')}")
            print("tickers:")
            for t in pack.get("ticker_rows") or []:
                mark = "ON" if t.get("subscribed") else "OFF"
                print(f"  [{mark}] {t.get('symbol')}" + (" ·extra" if t.get("is_extra") else ""))

    if args.feed:
        feed = load_sa_feed(hours=72, limit=30)
        print(f"feed count={feed.get('count')} source={feed.get('source')}")
        for it in (feed.get("items") or [])[:15]:
            print(f"  {it.get('symbol')}: {(it.get('title') or '')[:80]}")

    if args.ingest or args.ingest_sections_only or args.ingest_tickers_only:
        lim = int(args.limit) if int(args.limit or 0) > 0 else None
        result = run_sa_ingest(
            include_tickers=not args.ingest_sections_only,
            include_sections=not args.ingest_tickers_only,
            all_sections=bool(args.all_sections),
            limit=lim,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"kb_inserted_total={result.get('kb_inserted_total')}")
            tp = result.get("tickers") or {}
            sp = result.get("sections") or {}
            print(f"  tickers: n={len(tp.get('tickers') or [])} kb+{tp.get('kb_inserted')}")
            print(f"  sections: items={sp.get('item_count')} kb+{sp.get('kb_inserted')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

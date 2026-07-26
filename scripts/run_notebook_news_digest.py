#!/usr/bin/env python3
"""Fetch SA news → knowledge_base → notebook digest (ProxyAPI LLM).

Examples:
  python scripts/run_notebook_news_digest.py --tickers MSFT,SNDK --no-llm
  python scripts/run_notebook_news_digest.py --from-kb-only --no-llm
  python scripts/run_notebook_news_digest.py --max-tickers 8
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Notebook SA news → KB → digest")
    p.add_argument("--tickers", default="", help="Comma list; default = portfolio ∪ GAME_5M equities")
    p.add_argument("--max-tickers", type=int, default=0, help="Cap tickers (0 = no cap / use config)")
    p.add_argument("--per-ticker", type=int, default=0, help="News per ticker on SA fetch (0 = config)")
    p.add_argument("--sleep", type=float, default=-1.0, help="Sleep between tickers; <0 = config")
    p.add_argument("--lookback-hours", type=int, default=0, help="KB lookback for digest (0 = config)")
    p.add_argument("--no-llm", action="store_true", help="Skip ProxyAPI LLM")
    p.add_argument("--no-write", action="store_true", help="Do not write local/notebook/*.json cache")
    p.add_argument("--from-kb-only", action="store_true", help="Do not call SA API; digest from knowledge_base only")
    p.add_argument("--no-kb", action="store_true", help="Fetch SA but do not INSERT into knowledge_base")
    p.add_argument("--universe-only", action="store_true", help="Print universe and exit")
    args = p.parse_args(argv)

    from services.notebook_news_digest import build_news_universe, run_notebook_news_digest

    if args.universe_only:
        uni = build_news_universe()
        print(json.dumps(uni, ensure_ascii=False, indent=2))
        return 0

    tickers = [x.strip().upper() for x in (args.tickers or "").split(",") if x.strip()] or None
    kwargs: dict = {
        "tickers": tickers,
        "use_llm": not args.no_llm,
        "write": not args.no_write,
        "fetch_sa": not args.from_kb_only,
        "save_kb": not args.no_kb and not args.from_kb_only,
        "from_kb": True,
    }
    if args.no_kb and not args.from_kb_only:
        kwargs["from_kb"] = False  # digest from API payload only
    if args.max_tickers > 0:
        kwargs["max_tickers"] = args.max_tickers
    if args.per_ticker > 0:
        kwargs["per_ticker"] = args.per_ticker
    if args.sleep >= 0:
        kwargs["sleep_sec"] = args.sleep
    if args.lookback_hours > 0:
        kwargs["lookback_hours"] = args.lookback_hours

    result = run_notebook_news_digest(**kwargs)
    dig = result.get("digest") or {}
    pipe = result.get("pipeline") or {}
    raw = result.get("raw") or {}
    print(
        f"universe={len((result.get('universe') or {}).get('group3_union') or [])} "
        f"requested={len(result.get('requested_tickers') or [])} "
        f"kb_inserted={pipe.get('kb_inserted')} "
        f"items={raw.get('item_count')} from={raw.get('items_from')} "
        f"kept={dig.get('kept')} trashed={dig.get('trashed')}"
    )
    for label in ("signals", "risks", "macro", "newtickers"):
        rows = dig.get(label) or []
        print(f"  {label}: {len(rows)}")
        for row in rows[:5]:
            if isinstance(row, dict):
                print(f"    - {row.get('sym')}: {str(row.get('text') or '')[:120]}")
    if result.get("wrote"):
        print("wrote", result["wrote"])
    if dig.get("llm_error"):
        print("LLM_ERROR", dig["llm_error"], file=sys.stderr)
        return 3
    errs = raw.get("errors") or {}
    if errs:
        print(f"fetch_errors={len(errs)} sample={list(errs.items())[:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Smoke: Seeking Alpha Finance (RapidAPI tipsters) news by ticker.

Requires env RAPIDAPI_KEY (or SEEKING_ALPHA_RAPIDAPI_KEY).
Host: seeking-alpha-finance.p.rapidapi.com

Example:
  RAPIDAPI_KEY=... python scripts/seeking_alpha_finance_smoke.py --ticker MSFT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

HOST = "seeking-alpha-finance.p.rapidapi.com"
BASE = f"https://{HOST}"


def _api_key() -> str:
    return (
        os.environ.get("SEEKING_ALPHA_RAPIDAPI_KEY")
        or os.environ.get("RAPIDAPI_KEY")
        or ""
    ).strip()


def fetch_symbol_news(
    ticker: str,
    *,
    category: str = "all",
    page_number: int = 1,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    key = (api_key or _api_key()).strip()
    if not key:
        raise SystemExit("Set RAPIDAPI_KEY or SEEKING_ALPHA_RAPIDAPI_KEY")
    slug = urllib.parse.quote(str(ticker).strip().lower())
    qs = urllib.parse.urlencode(
        {
            "category": category,
            "ticker_slug": slug,
            "page_number": int(page_number),
        }
    )
    url = f"{BASE}/v1/symbols/news?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "x-rapidapi-host": HOST,
            "x-rapidapi-key": key,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code}: {err_body[:500]}") from e
    data = json.loads(body)
    return {"status": status, "url": url, "payload": data}


def summarize_items(payload: Dict[str, Any], *, limit: int = 8) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for item in (payload.get("data") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "type": str(item.get("type") or ""),
                "publishOn": str(attrs.get("publishOn") or ""),
                "title": str(attrs.get("title") or ""),
            }
        )
    return rows


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SA Finance RapidAPI smoke by ticker")
    p.add_argument("--ticker", default="MSFT")
    p.add_argument("--category", default="all")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--json", action="store_true", help="print full JSON payload")
    args = p.parse_args(argv)

    out = fetch_symbol_news(args.ticker, category=args.category, page_number=args.page)
    items = summarize_items(out["payload"], limit=args.limit)
    print(f"ticker={args.ticker.upper()} status={out['status']} items={len(items)}")
    print(f"url={out['url']}")
    for i, row in enumerate(items, 1):
        print(f"{i}. [{row['publishOn']}] {row['title']} (id={row['id']})")
    if args.json:
        print(json.dumps(out["payload"], ensure_ascii=False, indent=2)[:8000])
    if not items:
        print("WARN: empty list", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

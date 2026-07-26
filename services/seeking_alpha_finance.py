"""Seeking Alpha Finance via RapidAPI (tipsters host).

Env: SEEKING_ALPHA_RAPIDAPI_KEY or RAPIDAPI_KEY.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from config_loader import get_config_value

logger = logging.getLogger(__name__)

HOST = "seeking-alpha-finance.p.rapidapi.com"
BASE = f"https://{HOST}"


def rapidapi_key() -> str:
    return (
        (get_config_value("SEEKING_ALPHA_RAPIDAPI_KEY") or "").strip()
        or (get_config_value("RAPIDAPI_KEY") or "").strip()
        or (os.environ.get("SEEKING_ALPHA_RAPIDAPI_KEY") or "").strip()
        or (os.environ.get("RAPIDAPI_KEY") or "").strip()
    )


def fetch_symbol_news(
    ticker: str,
    *,
    category: str = "all",
    page_number: int = 1,
    api_key: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """GET /v1/symbols/news — news list for ticker_slug."""
    key = (api_key or rapidapi_key()).strip()
    if not key:
        raise ValueError("SEEKING_ALPHA_RAPIDAPI_KEY / RAPIDAPI_KEY not set")
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        status = int(getattr(resp, "status", 200) or 200)
    payload = json.loads(body)
    return {"status": status, "url": url, "payload": payload}


def flatten_news_items(payload: Dict[str, Any], *, ticker: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Normalize SA Finance news payload into compact rows."""
    rows: List[Dict[str, Any]] = []
    for item in (payload.get("data") or [])[: max(0, int(limit))]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        content = str(attrs.get("content") or "")
        # Strip tags lightly for LLM context
        text = content
        for tag in ("<p>", "</p>", "<span>", "</span>", "<br>", "<br/>"):
            text = text.replace(tag, " ")
        while "<" in text and ">" in text:
            a = text.find("<")
            b = text.find(">", a)
            if b < 0:
                break
            text = text[:a] + " " + text[b + 1 :]
        text = " ".join(text.split())
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "type": str(item.get("type") or "news"),
                "ticker": str(ticker).upper(),
                "publishOn": str(attrs.get("publishOn") or ""),
                "title": str(attrs.get("title") or ""),
                "summary_text": text[:900],
                "isPaywalled": bool(attrs.get("isPaywalled")),
                "link": f"https://seekingalpha.com/news/{item.get('id')}" if item.get("id") else "",
                "src": "Seeking Alpha Finance / RapidAPI",
            }
        )
    return rows


def fetch_news_for_tickers(
    tickers: Sequence[str],
    *,
    per_ticker: int = 5,
    category: str = "all",
    sleep_sec: float = 0.35,
    api_key: Optional[str] = None,
    max_tickers: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch news for many tickers; continue on per-ticker errors."""
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if max_tickers is not None:
        wanted = wanted[: max(0, int(max_tickers))]
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    all_items: List[Dict[str, Any]] = []
    for i, t in enumerate(wanted):
        try:
            raw = fetch_symbol_news(t, category=category, page_number=1, api_key=api_key)
            items = flatten_news_items(raw["payload"], ticker=t, limit=per_ticker)
            by_ticker[t] = items
            all_items.extend(items)
        except Exception as e:
            logger.warning("SA news %s failed: %s", t, e)
            errors[t] = str(e)
            by_ticker[t] = []
        if sleep_sec > 0 and i + 1 < len(wanted):
            time.sleep(float(sleep_sec))
    return {
        "tickers": wanted,
        "by_ticker": by_ticker,
        "items": all_items,
        "errors": errors,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

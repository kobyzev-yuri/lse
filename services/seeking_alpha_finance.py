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
KB_SOURCE = "Seeking Alpha Finance"


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
                "src": KB_SOURCE,
            }
        )
    return rows


def _parse_publish_on(s: str):
    from datetime import datetime, timezone

    raw = (s or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def items_to_kb_articles(items: Sequence[Dict[str, Any]], *, exchange: str = "NYSE"):
    """Map flattened SA items → ticker_news_merge Article for knowledge_base."""
    from services.kb_extended_fields import kb_content_sha256
    from services.ticker_news_merge_fetcher import Article

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("ticker") or "").strip().upper()
        title = str(it.get("title") or "").strip()
        if not sym or not title:
            continue
        link = str(it.get("link") or "").strip()
        summary = str(it.get("summary_text") or "").strip()
        sa_id = str(it.get("id") or "").strip()
        # Stable unique external_id (sha256 hex ≥24 chars for kb_resolved_external_id).
        ext_raw = kb_content_sha256(f"sa_finance|{sym}|{sa_id}|{link}|{title}")
        out.append(
            Article(
                ts=_parse_publish_on(str(it.get("publishOn") or "")),
                symbol=sym,
                exchange=(exchange or "NYSE").strip().upper()[:16],
                source=KB_SOURCE[:120],
                title=title[:2000],
                summary=summary[:4000],
                url=link[:2000],
                external_id_raw=ext_raw,
                raw_payload={"provider": "seeking_alpha_finance", "item": it},
            )
        )
    return out


def save_sa_items_to_kb(items: Sequence[Dict[str, Any]], *, exchange: str = "NYSE") -> int:
    """Insert SA news into knowledge_base (ON CONFLICT DO NOTHING)."""
    from services.ticker_news_merge_fetcher import save_articles_to_kb

    articles = items_to_kb_articles(items, exchange=exchange)
    if not articles:
        return 0
    return int(save_articles_to_kb(articles) or 0)


def fetch_and_save_sa_news(
    tickers: Sequence[str],
    *,
    per_ticker: int = 5,
    sleep_sec: float = 0.35,
    max_tickers: Optional[int] = None,
    exchange: str = "NYSE",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch SA Finance news for tickers and write new rows into knowledge_base."""
    bundle = fetch_news_for_tickers(
        tickers,
        per_ticker=per_ticker,
        sleep_sec=sleep_sec,
        max_tickers=max_tickers,
        api_key=api_key,
    )
    items = bundle.get("items") or []
    inserted = 0
    try:
        inserted = save_sa_items_to_kb(items, exchange=exchange)
    except Exception as e:
        logger.exception("SA → knowledge_base save failed: %s", e)
        bundle["kb_error"] = str(e)
    bundle["kb_inserted"] = inserted
    return bundle


def load_kb_news_items(
    tickers: Sequence[str],
    *,
    lookback_hours: int = 72,
    source: Optional[str] = KB_SOURCE,
    limit: int = 80,
) -> List[Dict[str, Any]]:
    """Load recent NEWS rows from knowledge_base for digest (same shape as flatten_news_items)."""
    from sqlalchemy import bindparam, create_engine, text

    from config_loader import get_database_url
    from services.kb_extended_fields import kb_legacy_ticker

    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not wanted:
        return []
    legacy = [kb_legacy_ticker(t) for t in wanted]
    legacy = [t for t in legacy if t]
    hours = max(1, int(lookback_hours))
    lim = max(1, min(int(limit), 500))
    src = (source or "").strip() or None

    sql = text(
        """
        SELECT id, ts, ticker, source, content, link, sentiment_score, insight,
               COALESCE(NULLIF(symbol, ''), ticker) AS sym
        FROM knowledge_base
        WHERE event_type = 'NEWS'
          AND ts >= (NOW() AT TIME ZONE 'utc') - make_interval(hours => :hours)
          AND (
                UPPER(TRIM(ticker)) IN :tickers
             OR UPPER(TRIM(COALESCE(symbol, ''))) IN :symbols
          )
          AND (:src IS NULL OR source = :src)
        ORDER BY ts DESC
        LIMIT :lim
        """
    ).bindparams(bindparam("tickers", expanding=True), bindparam("symbols", expanding=True))

    engine = create_engine(get_database_url())
    rows: List[Dict[str, Any]] = []
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sql,
                {
                    "hours": hours,
                    "tickers": legacy,
                    "symbols": wanted,
                    "src": src,
                    "lim": lim,
                },
            )
            for r in result.mappings():
                content = str(r.get("content") or "")
                title = content.split("\n\n", 1)[0].strip()[:500]
                body = ""
                if "\n\n" in content:
                    parts = content.split("\n\n")
                    body = " ".join(parts[1:]).strip()
                    # drop trailing URL line if present
                    if body and body.rsplit("\n", 1)[-1].startswith("http"):
                        body = "\n".join(body.split("\n")[:-1]).strip()
                sym = str(r.get("sym") or r.get("ticker") or "").strip().upper()
                ts = r.get("ts")
                publish = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
                rows.append(
                    {
                        "id": str(r.get("id") or ""),
                        "kb_id": r.get("id"),
                        "type": "news",
                        "ticker": sym,
                        "publishOn": publish,
                        "title": title,
                        "summary_text": (body or title)[:900],
                        "link": str(r.get("link") or ""),
                        "src": str(r.get("source") or KB_SOURCE),
                        "sentiment_score": r.get("sentiment_score"),
                        "insight": r.get("insight"),
                    }
                )
    finally:
        engine.dispose()
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

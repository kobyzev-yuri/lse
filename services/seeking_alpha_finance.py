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


def _request_json(
    path_or_url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """GET tipsters SA Finance path (or absolute URL) → {status, url, payload}."""
    key = (api_key or rapidapi_key()).strip()
    if not key:
        raise ValueError("SEEKING_ALPHA_RAPIDAPI_KEY / RAPIDAPI_KEY not set")
    raw = str(path_or_url or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        url = raw
    else:
        path = raw if raw.startswith("/") else f"/{raw}"
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
        else:
            url = f"{BASE}{path}"
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
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        status = int(e.code)
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {"message": body[:500]}
        return {"status": status, "url": url, "payload": payload if isinstance(payload, dict) else {}}
    payload = json.loads(body)
    return {"status": status, "url": url, "payload": payload if isinstance(payload, dict) else {}}


def fetch_symbol_news(
    ticker: str,
    *,
    category: str = "all",
    page_number: int = 1,
    api_key: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """GET /v1/symbols/news — news list for ticker_slug."""
    slug = str(ticker).strip().lower()
    out = _request_json(
        "/v1/symbols/news",
        params={
            "category": category,
            "ticker_slug": slug,
            "page_number": int(page_number),
        },
        api_key=api_key,
        timeout=timeout,
    )
    status = int(out.get("status") or 0)
    if status != 200:
        msg = ""
        pl = out.get("payload") if isinstance(out.get("payload"), dict) else {}
        if isinstance(pl, dict):
            msg = str(pl.get("message") or pl.get("error") or "")[:300]
        raise RuntimeError(f"SA symbols/news HTTP {status}" + (f": {msg}" if msg else ""))
    return out


def fetch_section(
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """GET arbitrary tipsters section endpoint (articles/list, markets/day-watch, …)."""
    return _request_json(path, params=params, api_key=api_key, timeout=timeout)


def _strip_html(content: str) -> str:
    text = str(content or "")
    for tag in ("<p>", "</p>", "<span>", "</span>", "<br>", "<br/>"):
        text = text.replace(tag, " ")
    while "<" in text and ">" in text:
        a = text.find("<")
        b = text.find(">", a)
        if b < 0:
            break
        text = text[:a] + " " + text[b + 1 :]
    return " ".join(text.split())


def _ticker_slugs_from_relationships(item: Dict[str, Any]) -> List[str]:
    rel = item.get("relationships") if isinstance(item.get("relationships"), dict) else {}
    out: List[str] = []
    for key in ("primaryTickers", "secondaryTickers"):
        block = rel.get(key) if isinstance(rel.get(key), dict) else {}
        data = block.get("data")
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Tipsters often returns tag ids only; prefer explicit slug/name if present.
            slug = str(row.get("slug") or row.get("name") or "").strip()
            if slug and not slug.isdigit():
                out.append(slug.upper()[:16])
    # de-dupe preserve order
    seen: set[str] = set()
    uniq: List[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def flatten_section_items(
    payload: Dict[str, Any],
    *,
    section_id: str,
    limit: int = 40,
    item_kind: str = "articles",
) -> List[Dict[str, Any]]:
    """Normalize section payloads into compact rows (title/summary/link/…)."""
    lim = max(0, int(limit))
    sid = str(section_id or "").strip() or "section"
    kind = str(item_kind or "articles").strip().lower()
    rows: List[Dict[str, Any]] = []

    if kind == "day_watch":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
        buckets = (
            "in_the_news",
            "top_gainers",
            "top_losers",
            "sp500_gainers",
            "sp500_losers",
            "most_active",
            "faang_stocks",
            "cap400_gainers",
            "cap400_losers",
            "cap600_gainers",
            "cap600_losers",
            "cryptocurrencies",
        )
        for bucket in buckets:
            entries = attrs.get(bucket)
            if not isinstance(entries, list):
                continue
            for ent in entries:
                if not isinstance(ent, dict):
                    continue
                slug = str(ent.get("slug") or "").strip().lower()
                name = str(ent.get("name") or slug or "").strip()
                if not slug and not name:
                    continue
                eid = str(ent.get("id") or slug or name)
                rows.append(
                    {
                        "id": f"{bucket}:{eid}",
                        "type": "day_watch",
                        "section_id": sid,
                        "publishOn": "",
                        "title": f"{bucket}: {name}" + (f" ({slug.upper()})" if slug else ""),
                        "summary_text": name[:900],
                        "isPaywalled": False,
                        "link": f"https://seekingalpha.com/symbol/{slug}" if slug else "",
                        "tickers": [slug.upper()] if slug else [],
                        "src": KB_SOURCE,
                        "bucket": bucket,
                    }
                )
                if len(rows) >= lim:
                    return rows
        return rows

    # Default: JSON:API list under data[] (articles / news-like).
    for item in (payload.get("data") or [])[:lim]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        self_path = str(links.get("self") or "").strip()
        item_id = str(item.get("id") or "").strip()
        item_type = str(item.get("type") or kind or "article").strip()
        if self_path.startswith("http"):
            link = self_path
        elif self_path.startswith("/"):
            link = f"https://seekingalpha.com{self_path}"
        elif item_id and item_type == "news":
            link = f"https://seekingalpha.com/news/{item_id}"
        elif item_id:
            link = f"https://seekingalpha.com/article/{item_id}"
        else:
            link = ""
        summary = _strip_html(
            str(
                attrs.get("summary")
                or attrs.get("content")
                or attrs.get("description")
                or (
                    ", ".join(str(x) for x in attrs.get("themes"))
                    if isinstance(attrs.get("themes"), list)
                    else attrs.get("themes")
                )
                or ""
            )
        )
        rows.append(
            {
                "id": item_id,
                "type": item_type,
                "section_id": sid,
                "publishOn": str(attrs.get("publishOn") or attrs.get("lastModified") or ""),
                "title": str(attrs.get("title") or "").strip(),
                "summary_text": summary[:900],
                "isPaywalled": bool(attrs.get("isPaywalled") or attrs.get("isLockedPro")),
                "link": link,
                "tickers": _ticker_slugs_from_relationships(item),
                "src": KB_SOURCE,
            }
        )
    return rows


def fetch_section_pages(
    path: str,
    *,
    section_id: str,
    params: Optional[Dict[str, Any]] = None,
    limit: int = 40,
    item_kind: str = "articles",
    page_param: str = "page_number",
    start_page: int = 1,
    max_pages: int = 5,
    sleep_sec: float = 0.25,
    api_key: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Paginate a section endpoint until ``limit`` items or pages exhausted."""
    lim = max(0, int(limit))
    pages = max(1, int(max_pages))
    base_params = dict(params or {})
    all_items: List[Dict[str, Any]] = []
    errors: List[str] = []
    urls: List[str] = []
    last_status = 0
    kind = str(item_kind or "articles").strip().lower()

    for i in range(pages):
        if lim and len(all_items) >= lim:
            break
        page_no = int(start_page) + i
        call_params = dict(base_params)
        if kind != "day_watch" and page_param:
            call_params[page_param] = page_no
        try:
            raw = fetch_section(path, params=call_params, api_key=api_key, timeout=timeout)
        except Exception as e:
            errors.append(str(e))
            break
        last_status = int(raw.get("status") or 0)
        urls.append(str(raw.get("url") or ""))
        if last_status != 200:
            msg = ""
            pl = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            if isinstance(pl, dict):
                msg = str(pl.get("message") or pl.get("error") or "")[:200]
            errors.append(f"HTTP {last_status}" + (f": {msg}" if msg else ""))
            break
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        need = lim - len(all_items) if lim else 40
        batch = flatten_section_items(payload, section_id=section_id, limit=need, item_kind=kind)
        if not batch:
            break
        all_items.extend(batch)
        if kind == "day_watch":
            break  # single payload, no pagination
        if sleep_sec > 0 and i + 1 < pages and (not lim or len(all_items) < lim):
            time.sleep(float(sleep_sec))

    return {
        "section_id": section_id,
        "status": last_status,
        "items": all_items[:lim] if lim else all_items,
        "count": len(all_items[:lim] if lim else all_items),
        "urls": urls,
        "errors": errors,
        "path": path,
        "params": base_params,
    }


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
        section_id = str(it.get("section_id") or "").strip()
        # Stable unique external_id (sha256 hex ≥24 chars for kb_resolved_external_id).
        ext_raw = kb_content_sha256(f"sa_finance|{sym}|{sa_id}|{link}|{title}")
        raw_payload: Dict[str, Any] = {"provider": "seeking_alpha_finance", "item": it}
        if section_id:
            raw_payload["sa_section"] = section_id
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
                raw_payload=raw_payload,
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
    per_ticker: int = 40,
    per_ticker_limits: Optional[Dict[str, int]] = None,
    sleep_sec: float = 0.35,
    max_tickers: Optional[int] = None,
    exchange: str = "NYSE",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch SA Finance news for tickers and write new rows into knowledge_base."""
    bundle = fetch_news_for_tickers(
        tickers,
        per_ticker=per_ticker,
        per_ticker_limits=per_ticker_limits,
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
                        "sentiment_score": float(r["sentiment_score"])
                        if r.get("sentiment_score") is not None
                        else None,
                        "insight": r.get("insight"),
                    }
                )
    finally:
        engine.dispose()
    return rows


def load_kb_earnings_items(
    tickers: Sequence[str],
    *,
    days_back: int = 7,
    days_ahead: int = 45,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    """Upcoming/recent EARNINGS rows (Yahoo/yfinance etc.) for notebook digest context."""
    from sqlalchemy import bindparam, create_engine, text

    from config_loader import get_database_url
    from services.kb_extended_fields import kb_legacy_ticker

    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not wanted:
        return []
    legacy = [kb_legacy_ticker(t) for t in wanted]
    legacy = [t for t in legacy if t]
    back = max(0, int(days_back))
    ahead = max(1, int(days_ahead))
    lim = max(1, min(int(limit), 200))

    sql = text(
        """
        SELECT id, ts, ticker, source, content, link,
               COALESCE(NULLIF(symbol, ''), ticker) AS sym
        FROM knowledge_base
        WHERE event_type = 'EARNINGS'
          AND ts >= (CURRENT_DATE - make_interval(days => :back))
          AND ts <= (CURRENT_DATE + make_interval(days => :ahead))
          AND (
                UPPER(TRIM(ticker)) IN :tickers
             OR UPPER(TRIM(COALESCE(symbol, ''))) IN :symbols
          )
        ORDER BY ts ASC
        LIMIT :lim
        """
    ).bindparams(bindparam("tickers", expanding=True), bindparam("symbols", expanding=True))

    engine = create_engine(get_database_url())
    rows: List[Dict[str, Any]] = []
    try:
        with engine.connect() as conn:
            result = conn.execute(
                sql,
                {"back": back, "ahead": ahead, "tickers": legacy, "symbols": wanted, "lim": lim},
            )
            for r in result.mappings():
                content = str(r.get("content") or "")
                title = content.split("\n", 1)[0].strip()[:300]
                body = content.strip()[:500]
                sym = str(r.get("sym") or r.get("ticker") or "").strip().upper()
                ts = r.get("ts")
                publish = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
                rows.append(
                    {
                        "id": f"earn-{r.get('id')}",
                        "kb_id": r.get("id"),
                        "type": "earnings",
                        "ticker": sym,
                        "publishOn": publish,
                        "title": title or f"Earnings {sym}",
                        "summary_text": body,
                        "link": str(r.get("link") or ""),
                        "src": str(r.get("source") or "Yahoo Finance (yfinance)"),
                    }
                )
    finally:
        engine.dispose()
    return rows


def fetch_news_for_tickers(
    tickers: Sequence[str],
    *,
    per_ticker: int = 40,
    per_ticker_limits: Optional[Dict[str, int]] = None,
    category: str = "all",
    sleep_sec: float = 0.35,
    api_key: Optional[str] = None,
    max_tickers: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch news for many tickers; continue on per-ticker errors.

    ``per_ticker_limits`` overrides the default ``per_ticker`` for specific symbols.
    """
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if max_tickers is not None:
        wanted = wanted[: max(0, int(max_tickers))]
    limits = {str(k).strip().upper(): int(v) for k, v in (per_ticker_limits or {}).items() if str(k).strip()}
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    all_items: List[Dict[str, Any]] = []
    for i, t in enumerate(wanted):
        lim = int(limits.get(t, per_ticker))
        lim = max(0, lim)
        try:
            raw = fetch_symbol_news(t, category=category, page_number=1, api_key=api_key)
            items = flatten_news_items(raw["payload"], ticker=t, limit=lim)
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
        "per_ticker_default": int(per_ticker),
        "per_ticker_limits": limits,
    }

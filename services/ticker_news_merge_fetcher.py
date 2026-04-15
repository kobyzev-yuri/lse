"""
NYSE-style тикерные новости для LSE: Yahoo (обязательно) + Marketaux (если задан ключ),
merge/dedup и запись в knowledge_base с расширенными полями.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests
import yfinance as yf
from sqlalchemy import create_engine, text

from config_loader import get_config_value, get_database_url
from services.http_outbound import outbound_session
from services.kb_extended_fields import (
    kb_content_sha256,
    kb_legacy_ticker,
    kb_resolved_external_id,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Article:
    ts: datetime
    symbol: str
    exchange: str
    source: str
    title: str
    summary: str
    url: str
    external_id_raw: str
    raw_payload: Dict[str, Any]

    def content(self) -> str:
        parts = [self.title.strip()] if self.title else []
        if self.summary and self.summary.strip() and self.summary.strip() != self.title.strip():
            parts.append(self.summary.strip())
        if self.url:
            parts.append(self.url.strip())
        return "\n\n".join([p for p in parts if p]).strip()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Marketaux: "2024-11-08T01:24:00.000000Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _dedupe_key(a: Article) -> tuple[str, str]:
    return (a.symbol.strip().upper(), (a.url or "").strip())


def fetch_yahoo_news(
    tickers: Iterable[str],
    lookback_hours: int,
    max_per_ticker: int,
    exchange: str,
) -> List[Article]:
    out: List[Article] = []
    cutoff = _utcnow() - timedelta(hours=float(max(1, lookback_hours)))
    for t in tickers:
        sym = (t or "").strip().upper()
        if not sym:
            continue
        try:
            raw_news = yf.Ticker(sym).get_news(count=max(1, int(max_per_ticker))) or []
        except Exception as exc:
            log.warning("Yahoo news failed ticker=%s: %s", sym, exc)
            continue
        for item in raw_news:
            content = (item or {}).get("content") or {}
            title = (content.get("title") or "").strip()
            pub_date = (content.get("pubDate") or "").strip()
            if not title or not pub_date:
                continue
            ts = _parse_dt(pub_date) or _utcnow()
            if ts < cutoff:
                continue
            provider = content.get("provider") or {}
            click = content.get("clickThroughUrl") or {}
            canonical = content.get("canonicalUrl") or {}
            url = (click.get("url") or canonical.get("url") or "").strip()
            out.append(
                Article(
                    ts=ts,
                    symbol=sym,
                    exchange=(exchange or "").strip().upper()[:16],
                    source=(provider.get("displayName") or "Yahoo Finance").strip()[:120],
                    title=title[:2000],
                    summary=(content.get("summary") or "").strip()[:4000],
                    url=url[:2000],
                    external_id_raw="yfinance",
                    raw_payload={"provider": "yfinance", "item": item},
                )
            )
    return out


def fetch_marketaux_news(
    api_token: str,
    tickers: Iterable[str],
    lookback_hours: int,
    exchange: str,
    limit: int = 100,
) -> List[Article]:
    symbols = [((t or "").strip().upper()) for t in tickers if (t or "").strip()]
    symbols = [s for s in symbols if s not in ("MACRO", "US_MACRO", "CASH")]
    if not symbols:
        return []

    cutoff = _utcnow() - timedelta(hours=float(max(1, lookback_hours)))
    published_after = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    sess = outbound_session("MARKETAUX_USE_SYSTEM_PROXY")
    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "api_token": api_token,
        "symbols": ",".join(symbols[:50]),
        "filter_entities": "true",
        "group_similar": "true",
        "language": "en",
        "published_after": published_after,
        "limit": str(max(10, min(int(limit), 200))),
        "page": "1",
    }

    try:
        r = sess.get(url, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as exc:
        log.warning("Marketaux request failed: %s", exc)
        return []
    except Exception as exc:
        log.warning("Marketaux decode failed: %s", exc)
        return []

    data = payload.get("data") or []
    out: List[Article] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        if not title:
            continue
        ts = _parse_dt(str(row.get("published_at") or "")) or _utcnow()
        if ts < cutoff:
            continue
        url_row = (row.get("url") or "").strip()
        uuid = (row.get("uuid") or "").strip()
        desc = (row.get("description") or row.get("snippet") or "").strip()
        src = (row.get("source") or "marketaux").strip()
        entities = row.get("entities") or []
        ent_symbols = {((e or {}).get("symbol") or "").strip().upper() for e in entities if isinstance(e, dict)}
        hit = sorted(ent_symbols.intersection(set(symbols)))
        if not hit:
            continue
        for sym in hit[:3]:
            out.append(
                Article(
                    ts=ts,
                    symbol=sym,
                    exchange=(exchange or "").strip().upper()[:16],
                    source=src[:120],
                    title=title[:2000],
                    summary=desc[:4000],
                    url=url_row[:2000],
                    external_id_raw=uuid or "marketaux",
                    raw_payload={"provider": "marketaux", "row": row},
                )
            )
    return out


def merge_articles(*batches: List[Article]) -> List[Article]:
    """
    Простой merge/dedup как база: уникальность по (symbol, url), если url пустой — по (symbol, title+ts).
    При конфликте предпочтение первому батчу (Yahoo идёт первым → устойчиво).
    """
    seen: set[tuple[str, str]] = set()
    out: List[Article] = []
    for batch in batches:
        for a in batch:
            if a.url:
                k = _dedupe_key(a)
            else:
                k = (a.symbol.strip().upper(), f"t:{a.title.strip().lower()}|{a.ts.isoformat()}")
            if k in seen:
                continue
            seen.add(k)
            out.append(a)
    out.sort(key=lambda x: x.ts, reverse=True)
    return out


def save_articles_to_kb(articles: List[Article]) -> int:
    if not articles:
        return 0
    engine = create_engine(get_database_url())
    inserted = 0

    sql = text(
        """
        INSERT INTO knowledge_base
          (ts, ticker, source, content, event_type, importance, link, ingested_at,
           exchange, symbol, external_id, content_sha256, raw_payload)
        VALUES
          (:ts, :ticker, :source, :content, :event_type, :importance, :link, NOW(),
           :exchange, :symbol, :external_id, :content_sha256, CAST(:raw_payload AS jsonb))
        ON CONFLICT DO NOTHING
        """
    )

    with engine.begin() as conn:
        for a in articles:
            content = a.content()[:8000]
            if not content:
                continue
            ext = kb_resolved_external_id(a.external_id_raw, a.exchange, a.symbol, a.url, a.title)[:512]
            params = {
                "ts": a.ts,
                "ticker": kb_legacy_ticker(a.symbol),
                "source": (a.source or "News")[:120],
                "content": content,
                "event_type": "NEWS",
                "importance": "MEDIUM",
                "link": (a.url or "").strip()[:2000],
                "exchange": (a.exchange or "").strip().upper()[:16],
                "symbol": (a.symbol or "").strip().upper()[:64],
                "external_id": ext,
                "content_sha256": kb_content_sha256(content),
                "raw_payload": json.dumps(a.raw_payload, ensure_ascii=False),
            }
            res = conn.execute(sql, params)
            if res.rowcount and res.rowcount > 0:
                inserted += 1

    engine.dispose()
    return inserted


def fetch_and_save_ticker_news() -> int:
    """
    Главный entrypoint для cron: merge Yahoo + Marketaux и сохранить новые строки в KB.

    Конфиг:
      - TICKER_NEWS_TICKERS: список тикеров (по умолчанию TICKERS_FAST)
      - TICKER_NEWS_LOOKBACK_HOURS: окно (по умолчанию 48)
      - TICKER_NEWS_MAX_PER_TICKER: cap для Yahoo (по умолчанию 40)
      - TICKER_NEWS_EXCHANGE: метка exchange (по умолчанию NYSE)
      - MARKETAUX_API_KEY: ключ Marketaux (если задан — добавляем источник)
    """
    tickers_raw = (get_config_value("TICKER_NEWS_TICKERS", "") or "").strip()
    if not tickers_raw:
        tickers_raw = (get_config_value("TICKERS_FAST", "") or "").strip()
    tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
    if not tickers:
        return 0

    try:
        lookback_hours = int((get_config_value("TICKER_NEWS_LOOKBACK_HOURS", "48") or "48").strip())
    except (ValueError, TypeError):
        lookback_hours = 48
    try:
        max_per_ticker = int((get_config_value("TICKER_NEWS_MAX_PER_TICKER", "40") or "40").strip())
    except (ValueError, TypeError):
        max_per_ticker = 40
    exchange = (get_config_value("TICKER_NEWS_EXCHANGE", "NYSE") or "NYSE").strip().upper()[:16]

    log.info("📰 Ticker news: tickers=%s lookback_hours=%s", tickers[:20], lookback_hours)
    yahoo = fetch_yahoo_news(tickers, lookback_hours=lookback_hours, max_per_ticker=max_per_ticker, exchange=exchange)
    log.info("📰 Ticker news: yahoo=%d", len(yahoo))

    mx_key = (get_config_value("MARKETAUX_API_KEY", "") or "").strip()
    marketaux: List[Article] = []
    if mx_key:
        marketaux = fetch_marketaux_news(mx_key, tickers, lookback_hours=lookback_hours, exchange=exchange, limit=120)
        log.info("📰 Ticker news: marketaux=%d", len(marketaux))
    else:
        log.info("📰 Ticker news: marketaux skipped (MARKETAUX_API_KEY not set)")

    merged = merge_articles(yahoo, marketaux)
    log.info("📰 Ticker news: merged=%d", len(merged))
    inserted = save_articles_to_kb(merged)
    log.info("✅ Ticker news: inserted=%d", inserted)
    return inserted


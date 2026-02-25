"""
Сбор новостей с Investing.com (лента stock-market-news).
Дополняет существующий источник Investing.com Economic Calendar.
Сохраняет в knowledge_base с source='Investing.com News', event_type='NEWS'.

Используются тикеры из TICKERS_FAST и встроенные ключевые слова по тикеру
(см. BUILTIN_KEYWORDS ниже). Опционально в config.env можно добавить свои ключевые слова:
  INVESTING_NEWS_TICKER_KEYWORDS=SNDK:Citron,short;LITE:opto
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text

from config_loader import get_config_value, get_database_url
from services.ticker_groups import get_tickers_fast

logger = logging.getLogger(__name__)

INVESTING_NEWS_URL = "https://www.investing.com/news/stock-market-news"
INVESTING_BASE_URL = "https://www.investing.com"

# Заголовки, максимально похожие на обычный браузер (снижает вероятность 403)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
    "Cache-Control": "max-age=0",
}

# Встроенные ключевые слова для сопоставления заголовка новости с тикером (TICKERS_FAST).
# Для тикера не из списка используется сам тикер как единственное слово.
BUILTIN_KEYWORDS: Dict[str, List[str]] = {
    "SNDK": ["SanDisk", "Western Digital", "WDC", "SNDK"],
    "NDK": ["NDK"],
    "LITE": ["LITE", "Lumentum"],
    "NBIS": ["NBIS"],
}

# Текущая структура: ссылки на новости в виде /news/stock-market-news/...
LINK_PATTERN = re.compile(r"^/news/[^/]+/.+", re.I)


def _ticker_keywords() -> Dict[str, List[str]]:
    """Тикеры из TICKERS_FAST и ключевые слова: встроенные + опционально из config."""
    tickers = get_tickers_fast()
    if not tickers:
        return {t: BUILTIN_KEYWORDS.get(t, [t]) for t in BUILTIN_KEYWORDS}
    # Базово: встроенные ключевые слова для каждого тикера из TICKERS_FAST
    out = {t: list(BUILTIN_KEYWORDS.get(t, [t])) for t in tickers}
    # Опционально: дополнение из config (формат SNDK:слово1,слово2;LITE:слово3)
    raw = get_config_value("INVESTING_NEWS_TICKER_KEYWORDS", "").strip()
    if raw:
        for part in raw.split(";"):
            part = part.strip()
            if ":" not in part:
                continue
            ticker, keys_str = part.split(":", 1)
            ticker = ticker.strip().upper()
            if ticker not in out:
                continue
            extra = [k.strip() for k in keys_str.split(",") if k.strip()]
            for k in extra:
                if k not in out[ticker]:
                    out[ticker].append(k)
    return out


def _link_already_in_kb(conn, link: str) -> bool:
    """Проверяет, есть ли уже запись с таким link в knowledge_base."""
    row = conn.execute(
        text("SELECT 1 FROM knowledge_base WHERE link = :link LIMIT 1"),
        {"link": link[:2000]},
    ).fetchone()
    return row is not None


def _session_with_proxy() -> requests.Session:
    """Сессия с браузерными заголовками и опциональным прокси (config: INVESTING_NEWS_PROXY)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = f"{INVESTING_BASE_URL}/"
    proxy = get_config_value("INVESTING_NEWS_PROXY", "").strip() or None
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def fetch_investing_news_list(max_articles: int = 30) -> List[Tuple[str, str]]:
    """
    Загружает ленту stock-market-news с Investing.com.
    Использует Session и браузерные заголовки; при 403 можно задать INVESTING_NEWS_PROXY в config.env.
    """
    session = _session_with_proxy()
    try:
        # Сначала главная страница — часто снимает 403 на дочерних (cookies)
        session.get(INVESTING_BASE_URL + "/", timeout=15)
    except Exception:
        pass
    try:
        resp = session.get(INVESTING_NEWS_URL, timeout=25)
        resp.raise_for_status()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logger.warning(
                "Investing.com news: 403 Forbidden. Сайт блокирует запросы. Варианты: "
                "1) Задать HTTP(S)-прокси в config.env: INVESTING_NEWS_PROXY=http://user:pass@host:port  2) Отключить источник в cron."
            )
        else:
            logger.warning("Investing.com news: запрос не удался: %s", e)
        return []
    except Exception as e:
        logger.warning("Investing.com news: запрос не удался: %s", e)
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    results: List[Tuple[str, str]] = []

    # Типичные селекторы: ссылки на /news/... внутри статей или списков
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not LINK_PATTERN.match(href):
            continue
        title = (a.get_text() or "").strip()
        if len(title) < 10 or len(title) > 500:
            continue
        full_url = href if href.startswith("http") else urljoin(INVESTING_BASE_URL + "/", href)
        results.append((title, full_url))
        if len(results) >= max_articles:
            break

    # Дедупликация по URL
    seen = set()
    unique = []
    for t, u in results:
        if u not in seen:
            seen.add(u)
            unique.append((t, u))
    logger.info("Investing.com news: получено %s заголовков", len(unique))
    return unique


def _match_ticker(title: str, ticker_keywords: Dict[str, List[str]]) -> Optional[str]:
    """По заголовку возвращает тикер, если есть совпадение по ключевым словам."""
    title_lower = title.lower()
    for ticker, keywords in ticker_keywords.items():
        for kw in keywords:
            if kw and kw.lower() in title_lower:
                return ticker
    return None


def fetch_and_save_investing_news(max_articles: int = 25) -> int:
    """
    Скачивает ленту новостей Investing.com, сопоставляет с тикерами по ключевым словам,
    сохраняет новые в knowledge_base. Возвращает количество добавленных записей.
    """
    keyword_map = _ticker_keywords()
    items = fetch_investing_news_list(max_articles=max_articles)
    if not items:
        return 0

    engine = create_engine(get_database_url())
    added = 0
    with engine.begin() as conn:
        for title, url in items:
            if _link_already_in_kb(conn, url):
                continue
            ticker = _match_ticker(title, keyword_map)
            if not ticker:
                ticker = "MACRO"
            content = title
            try:
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base (ts, ticker, source, content, event_type, importance, link)
                        VALUES (:ts, :ticker, :source, :content, 'NEWS', 'MEDIUM', :link)
                    """),
                    {
                        "ts": datetime.now(),
                        "ticker": ticker,
                        "source": "Investing.com News",
                        "content": content[:4000],
                        "link": url[:2000],
                    },
                )
                added += 1
            except Exception as e:
                logger.debug("Не удалось вставить новость %s: %s", url[:50], e)
    if added:
        logger.info("Investing.com news: добавлено %s новостей в knowledge_base", added)
    return added

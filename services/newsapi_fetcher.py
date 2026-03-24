"""
Модуль для получения новостей через NewsAPI
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import os
import time
import requests
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value
from services.http_outbound import outbound_session

logger = logging.getLogger(__name__)

# Повторы при 429 (rate limit): паузы в секундах перед повторной попыткой
NEWSAPI_429_BACKOFF = [90, 180, 300]
NEWSAPI_429_MAX_RETRIES = 3

# Один объединённый запрос вместо 5 отдельных — иначе бесплатный tier (100 req/день) исчерпывается за минуты.
DEFAULT_MACRO_QUERY = (
    '("Federal Reserve" OR FOMC OR Fed OR "interest rate") OR '
    '("European Central Bank" OR ECB) OR ("Bank of England" OR BoE) OR '
    '(CPI OR inflation OR unemployment OR GDP) OR ("monetary policy")'
)


def _cooldown_file() -> Path:
    raw = (os.environ.get("NEWSAPI_COOLDOWN_FILE") or "").strip()
    if raw:
        return Path(raw)
    return project_root / "logs" / ".newsapi_cooldown_until"


def newsapi_cooldown_active() -> bool:
    """Если True — не дергать NewsAPI до истечения времени (после 429)."""
    p = _cooldown_file()
    if not p.exists():
        return False
    try:
        s = p.read_text(encoding="utf-8").strip()
        until = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    except Exception:
        return False


def newsapi_cooldown_until() -> Optional[datetime]:
    p = _cooldown_file()
    if not p.exists():
        return None
    try:
        s = p.read_text(encoding="utf-8").strip()
        until = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until
    except Exception:
        return None


def _set_newsapi_cooldown_after_429() -> None:
    """Пауза после исчерпания 429, чтобы cron не спамил логами и не жёг лимит."""
    try:
        hours = float((get_config_value("NEWSAPI_COOLDOWN_AFTER_429_HOURS", "12") or "12").strip())
    except (ValueError, TypeError):
        hours = 12.0
    hours = max(1.0, min(hours, 168.0))
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    path = _cooldown_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(until.isoformat(), encoding="utf-8")
    except OSError as e:
        logger.warning("NewsAPI: не удалось записать cooldown-файл %s: %s", path, e)


def _max_pages_config() -> int:
    try:
        v = int((get_config_value("NEWSAPI_MAX_PAGES", "1") or "1").strip())
        return max(1, min(v, 5))
    except (ValueError, TypeError):
        return 1


def _days_back_config() -> int:
    try:
        v = int((get_config_value("NEWSAPI_DAYS_BACK", "7") or "7").strip())
        return max(1, min(v, 30))
    except (ValueError, TypeError):
        return 7


def get_api_key() -> Optional[str]:
    """Получает API ключ NewsAPI из конфига"""
    return get_config_value('NEWSAPI_KEY', None)


def fetch_newsapi_articles(
    api_key: str,
    query: str,
    sources: Optional[str] = 'reuters,bloomberg,financial-times',
    language: str = 'en',
    days_back: int = 3,
    max_pages: int = 5,
) -> Tuple[List[Dict], bool]:
    """
    Получает новости через NewsAPI с пагинацией.

    Returns:
        (статьи, rate_limited): rate_limited=True если после 429 квота исчерпана — не дергать другие запросы в этом запуске.
    """
    url = "https://newsapi.org/v2/everything"
    from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    page_size = 100

    all_articles: List[Dict] = []
    page = 1
    http = outbound_session("NEWSAPI_USE_SYSTEM_PROXY")

    def _wait_429(response: requests.Response, attempt: int) -> Optional[float]:
        """Секунды ожидания перед повтором или None, если повторов больше нет."""
        if attempt >= NEWSAPI_429_MAX_RETRIES:
            return None
        ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if ra and str(ra).strip().isdigit():
            sec = min(max(int(ra), 30), 3600)
            logger.warning("NewsAPI 429, Retry-After=%s с (попытка %s)", sec, attempt + 1)
            return float(sec)
        bi = min(attempt, len(NEWSAPI_429_BACKOFF) - 1)
        w = float(NEWSAPI_429_BACKOFF[bi])
        logger.warning("NewsAPI 429 Too Many Requests, ждём %s с перед повтором (попытка %s)", w, attempt + 1)
        return w

    while page <= max_pages:
        params = {
            'q': query,
            'language': language,
            'sortBy': 'publishedAt',
            'apiKey': api_key,
            'from': from_date,
            'pageSize': page_size,
            'page': page
        }
        # Пустой/None — не ограничиваем источниками (нужно для тикерных запросов; иначе только 3 агентства).
        if sources and str(sources).strip():
            params['sources'] = sources

        last_err = None
        response = None
        for attempt in range(NEWSAPI_429_MAX_RETRIES + 1):
            try:
                response = http.get(url, params=params, timeout=45)
                if response.status_code == 429:
                    wait = _wait_429(response, attempt)
                    if wait is not None:
                        time.sleep(wait)
                        continue
                    _set_newsapi_cooldown_after_429()
                    logger.error(
                        "❌ NewsAPI: 429 после %s повторов (лимит запросов/день на плане). Cooldown %s ч — см. NEWSAPI_COOLDOWN_AFTER_429_HOURS.",
                        NEWSAPI_429_MAX_RETRIES + 1,
                        (get_config_value("NEWSAPI_COOLDOWN_AFTER_429_HOURS", "12") or "12").strip(),
                    )
                    return all_articles, True
                response.raise_for_status()
                break
            except requests.exceptions.HTTPError as e:
                last_err = e
                if e.response is not None and e.response.status_code == 429:
                    wait = _wait_429(e.response, attempt)
                    if wait is not None:
                        time.sleep(wait)
                        continue
                    _set_newsapi_cooldown_after_429()
                    logger.error("❌ NewsAPI: 429 после %s повторов.", NEWSAPI_429_MAX_RETRIES + 1)
                    return all_articles, True
                raise
            except requests.exceptions.RequestException as e:
                last_err = e
                break
        else:
            if last_err:
                raise last_err

        if response is None:
            break

        try:
            data = response.json()
            if data.get('status') != 'ok':
                logger.error("❌ NewsAPI ошибка: %s", data.get('message', 'Unknown error'))
                break

            articles_batch = data.get('articles', [])
            total_results = data.get('totalResults', 0)

            for article in articles_batch:
                try:
                    published_time = None
                    if article.get('publishedAt'):
                        try:
                            published_time = datetime.fromisoformat(
                                article['publishedAt'].replace('Z', '+00:00')
                            )
                        except Exception:
                            published_time = datetime.now()
                    all_articles.append({
                        'title': article.get('title', ''),
                        'content': (article.get('description', '') or '') + '\n\n' + (article.get('content', '') or ''),
                        'source': article.get('source', {}).get('name', 'Unknown'),
                        'published': published_time or datetime.now(),
                        'url': article.get('url', ''),
                        'author': article.get('author', '')
                    })
                except Exception as e:
                    logger.warning("⚠️ Ошибка парсинга статьи: %s", e)

            if len(articles_batch) < page_size or len(all_articles) >= total_results:
                break
            page += 1
            time.sleep(2)  # пауза между страницами (каждая страница = отдельный запрос к лимиту)

        except Exception as e:
            logger.warning("⚠️ Ошибка разбора ответа NewsAPI (страница %s): %s", page, e)
            break

    logger.info("✅ Получено %s новостей из NewsAPI для запроса '%s' (страниц: %s)", len(all_articles), query, page)
    return all_articles, False


LEGACY_MACRO_QUERIES = [
    'Federal Reserve OR FOMC OR Fed rate',
    'European Central Bank OR ECB',
    'Bank of England OR BoE',
    'CPI OR inflation OR unemployment OR GDP',
    'interest rate OR monetary policy',
]


def fetch_macro_news(api_key: str) -> Tuple[List[Dict], bool]:
    """
    Макро-новости (Fed, ECB, BoE, индикаторы).

    По умолчанию один объединённый запрос + max_pages=1 — иначе бесплатный tier NewsAPI
    (≈100 запросов/день) сжигается за один прогон cron.

    Returns:
        (уникальные статьи, rate_limited)
    """
    use_single = (get_config_value("NEWSAPI_MACRO_SINGLE_QUERY", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    dq = _days_back_config()
    mp = _max_pages_config()
    if use_single:
        q = (get_config_value("NEWSAPI_MACRO_QUERY", "") or "").strip()
        queries = [q if q else DEFAULT_MACRO_QUERY]
    else:
        queries = list(LEGACY_MACRO_QUERIES)

    all_news: List[Dict] = []
    rate_limited = False
    for query in queries:
        logger.info("🔍 Поиск новостей NewsAPI: %s", query[:200] + ("…" if len(query) > 200 else ""))
        news, rl = fetch_newsapi_articles(api_key, query, days_back=dq, max_pages=mp)
        all_news.extend(news)
        if rl:
            logger.warning("NewsAPI: цепочка макро-запросов прервана после 429")
            rate_limited = True
            break

    seen_urls = set()
    unique_news = []
    for item in all_news:
        if item.get('url') and item['url'] not in seen_urls:
            seen_urls.add(item['url'])
            unique_news.append(item)

    logger.info("✅ Всего уникальных макро-новостей NewsAPI: %s", len(unique_news))
    return unique_news, rate_limited


def fetch_equity_news(api_key: str) -> Tuple[List[Dict], bool]:
    """
    Второй запрос NewsAPI: заголовки по тикерам портфеля/быстрых (OR-запрос).
    Включается NEWSAPI_FETCH_EQUITY=true (+1 запрос к лимиту /everything).
    Источники не сужаем (sources не передаём), иначе тикерные новости почти пустые.
    """
    raw = (get_config_value("NEWSAPI_FETCH_EQUITY", "false") or "false").strip().lower()
    if raw not in ("1", "true", "yes"):
        return [], False

    tickers_src = (get_config_value("NEWSAPI_EQUITY_TICKERS", "") or "").strip()
    if not tickers_src:
        tickers_src = (get_config_value("EARNINGS_TRACK_TICKERS", "") or "").strip()
    if not tickers_src:
        tickers_src = (get_config_value("TICKERS_FAST", "SNDK,MU,MSFT") or "").strip()
    tickers = [t.strip() for t in tickers_src.split(",") if t.strip()][:12]

    parts: List[str] = []
    for t in tickers:
        u = t.upper()
        if u in ("MACRO", "US_MACRO", "CASH"):
            continue
        parts.append(f"({u} OR \"{u}\")")
    if not parts:
        return [], False

    q = " OR ".join(parts[:10])
    dq = _days_back_config()
    try:
        mp = int((get_config_value("NEWSAPI_EQUITY_MAX_PAGES", "2") or "2").strip())
        mp = max(1, min(mp, 5))
    except (ValueError, TypeError):
        mp = 2

    logger.info("🔍 NewsAPI equity: запрос по тикерам (%s …), days_back=%s, max_pages=%s", q[:120], dq, mp)
    return fetch_newsapi_articles(api_key, q, sources="", days_back=dq, max_pages=mp)


def save_news_to_db(news_items: List[Dict], ticker: str = 'MACRO', event_type: str = 'NEWS') -> int:
    """
    Сохраняет новости из NewsAPI в БД.

    Returns:
        Количество сохранённых записей.
    """
    if not news_items:
        return 0

    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    skipped_count = 0
    
    with engine.begin() as conn:
        for item in news_items:
            try:
                # Проверяем дубликаты по URL
                if item.get('url'):
                    existing = conn.execute(
                        text("""
                            SELECT id FROM knowledge_base 
                            WHERE link = :url
                        """),
                        {"url": item['url']}
                    ).fetchone()
                    
                    if existing:
                        skipped_count += 1
                        continue
                
                # Определяем регион по источнику/контенту
                region = None
                source_lower = item.get('source', '').lower()
                content_lower = item.get('content', '').lower()
                
                if 'federal reserve' in content_lower or 'fomc' in content_lower or 'fed' in content_lower:
                    region = 'USA'
                    ticker = 'US_MACRO'
                elif 'ecb' in content_lower or 'european central bank' in content_lower:
                    region = 'EU'
                elif 'bank of england' in content_lower or 'boe' in content_lower:
                    region = 'UK'
                
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base 
                        (ts, ticker, source, content, link, event_type, region, importance, ingested_at)
                        VALUES (:ts, :ticker, :source, :content, :link, :event_type, :region, :importance, NOW())
                    """),
                    {
                        "ts": item['published'],
                        "ticker": ticker,
                        "source": item.get('source', 'NewsAPI'),
                        "content": f"{item.get('title', '')}\n\n{item.get('content', '')}",
                        "link": item.get('url', ''),
                        "event_type": event_type,
                        "region": region,
                        "importance": "MEDIUM"  # Можно улучшить логику определения важности
                    }
                )
                saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении новости: {e}")
    
    logger.info("✅ Сохранено %s новостей из NewsAPI, пропущено дубликатов: %s", saved_count, skipped_count)
    engine.dispose()
    return saved_count


def fetch_and_save_newsapi_news() -> int:
    """
    Главная функция: получает новости из NewsAPI и сохраняет в БД
    """
    api_key = get_api_key()
    if not api_key:
        logger.warning("⚠️ NEWSAPI_KEY не настроен в config.env, пропускаем NewsAPI")
        return 0

    if newsapi_cooldown_active():
        until = newsapi_cooldown_until()
        logger.info(
            "NewsAPI: пропуск — cooldown после недавнего 429 до %s (UTC). Файл: %s — удалите для немедленного сброса.",
            until.isoformat() if until else "?",
            _cooldown_file(),
        )
        return 0

    logger.info("🚀 Начало получения новостей из NewsAPI")
    macro_news, macro_rl = fetch_macro_news(api_key)
    saved = 0
    if macro_news:
        saved += save_news_to_db(macro_news, ticker='MACRO', event_type='MACRO_NEWS')
    if macro_rl:
        logger.info("✅ Завершено: после 429 equity-запрос не выполняем")
        return saved
    if not macro_news:
        logger.info("ℹ️ NewsAPI макро: 0 статей (пустой ответ или фильтр)")

    eq_news, eq_rl = fetch_equity_news(api_key)
    if eq_news:
        saved += save_news_to_db(eq_news, ticker='MACRO', event_type='EQUITY_NEWS')
    if eq_rl:
        logger.warning("NewsAPI equity: 429 — часть лимита израсходована")

    logger.info("✅ Завершено получение новостей из NewsAPI, всего сохранено новых: %s", saved)
    return saved


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_newsapi_news()

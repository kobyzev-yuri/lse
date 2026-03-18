"""
Модуль для получения новостей через NewsAPI
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import time
import requests
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value

logger = logging.getLogger(__name__)

# Повторы при 429 (rate limit): паузы в секундах перед повторной попыткой
NEWSAPI_429_BACKOFF = [60, 120]
NEWSAPI_429_MAX_RETRIES = 2


def get_api_key() -> Optional[str]:
    """Получает API ключ NewsAPI из конфига"""
    return get_config_value('NEWSAPI_KEY', None)


def fetch_newsapi_articles(
    api_key: str,
    query: str,
    sources: str = 'reuters,bloomberg,financial-times',
    language: str = 'en',
    days_back: int = 3,
    max_pages: int = 5
) -> List[Dict]:
    """
    Получает новости через NewsAPI с пагинацией (задним числом за несколько дней).

    Args:
        api_key: API ключ NewsAPI
        query: Поисковый запрос (например, "Federal Reserve")
        sources: Источники через запятую
        language: Язык (en)
        days_back: Сколько дней назад искать (по умолчанию 3 — чтобы при разовом запуске cron подтянуть весь день с утра)
        max_pages: Максимум страниц по 100 статей (лимит API), чтобы не сжигать лимиты

    Returns:
        Список словарей с новостями
    """
    url = "https://newsapi.org/v2/everything"
    from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    page_size = 100

    all_articles = []
    page = 1

    while page <= max_pages:
        params = {
            'q': query,
            'sources': sources,
            'language': language,
            'sortBy': 'publishedAt',
            'apiKey': api_key,
            'from': from_date,
            'pageSize': page_size,
            'page': page
        }

        last_err = None
        response = None
        for attempt in range(NEWSAPI_429_MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, timeout=30)
                if response.status_code == 429:
                    if attempt < NEWSAPI_429_MAX_RETRIES:
                        wait = NEWSAPI_429_BACKOFF[attempt]
                        logger.warning("NewsAPI 429 Too Many Requests, ждём %s с перед повтором (попытка %s)", wait, attempt + 1)
                        time.sleep(wait)
                        continue
                    logger.error("❌ NewsAPI: 429 после %s повторов.", NEWSAPI_429_MAX_RETRIES + 1)
                    return all_articles
                response.raise_for_status()
                break
            except requests.exceptions.HTTPError as e:
                last_err = e
                if e.response is not None and e.response.status_code == 429 and attempt < NEWSAPI_429_MAX_RETRIES:
                    wait = NEWSAPI_429_BACKOFF[attempt]
                    logger.warning("NewsAPI 429, ждём %s с (попытка %s)", wait, attempt + 1)
                    time.sleep(wait)
                    continue
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
            time.sleep(1)  # небольшая пауза между страницами, чтобы не упереться в rate limit

        except Exception as e:
            logger.warning("⚠️ Ошибка разбора ответа NewsAPI (страница %s): %s", page, e)
            break

    logger.info("✅ Получено %s новостей из NewsAPI для запроса '%s' (страниц: %s)", len(all_articles), query, page)
    return all_articles


def fetch_macro_news(api_key: str) -> List[Dict]:
    """
    Получает макро-новости (Fed, ECB, BoE, экономические индикаторы)
    
    Args:
        api_key: API ключ NewsAPI
        
    Returns:
        Список новостей
    """
    queries = [
        'Federal Reserve OR FOMC OR Fed rate',
        'European Central Bank OR ECB',
        'Bank of England OR BoE',
        'CPI OR inflation OR unemployment OR GDP',
        'interest rate OR monetary policy'
    ]
    
    all_news = []
    for query in queries:
        logger.info("🔍 Поиск новостей: %s", query)
        news = fetch_newsapi_articles(api_key, query, days_back=3, max_pages=5)
        all_news.extend(news)
    
    # Удаляем дубликаты по URL
    seen_urls = set()
    unique_news = []
    for item in all_news:
        if item.get('url') and item['url'] not in seen_urls:
            seen_urls.add(item['url'])
            unique_news.append(item)
    
    logger.info(f"✅ Всего уникальных макро-новостей: {len(unique_news)}")
    return unique_news


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
                        (ts, ticker, source, content, link, event_type, region, importance)
                        VALUES (:ts, :ticker, :source, :content, :link, :event_type, :region, :importance)
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

    logger.info("🚀 Начало получения новостей из NewsAPI")
    macro_news = fetch_macro_news(api_key)
    if not macro_news:
        logger.info("✅ Завершено: NewsAPI вернул 0 статей")
        return 0
    saved = save_news_to_db(macro_news, ticker='MACRO', event_type='MACRO_NEWS')
    logger.info("✅ Завершено получение новостей из NewsAPI")
    return saved


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_newsapi_news()

"""
Модуль для получения новостей через NewsAPI
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import requests
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value

logger = logging.getLogger(__name__)


def get_api_key() -> Optional[str]:
    """Получает API ключ NewsAPI из конфига"""
    return get_config_value('NEWSAPI_KEY', None)


def fetch_newsapi_articles(
    api_key: str, 
    query: str, 
    sources: str = 'reuters,bloomberg,financial-times',
    language: str = 'en',
    days_back: int = 1
) -> List[Dict]:
    """
    Получает новости через NewsAPI
    
    Args:
        api_key: API ключ NewsAPI
        query: Поисковый запрос (например, "Federal Reserve")
        sources: Источники через запятую
        language: Язык (en)
        days_back: Сколько дней назад искать
        
    Returns:
        Список словарей с новостями
    """
    url = "https://newsapi.org/v2/everything"
    
    from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    
    params = {
        'q': query,
        'sources': sources,
        'language': language,
        'sortBy': 'publishedAt',
        'apiKey': api_key,
        'from': from_date,
        'pageSize': 100  # Максимум для бесплатного tier
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        if data.get('status') != 'ok':
            logger.error(f"❌ NewsAPI ошибка: {data.get('message', 'Unknown error')}")
            return []
        
        articles = []
        for article in data.get('articles', []):
            try:
                # Парсим дату
                published_time = None
                if article.get('publishedAt'):
                    try:
                        published_time = datetime.fromisoformat(
                            article['publishedAt'].replace('Z', '+00:00')
                        )
                    except:
                        published_time = datetime.now()
                
                articles.append({
                    'title': article.get('title', ''),
                    'content': (article.get('description', '') or '') + '\n\n' + (article.get('content', '') or ''),
                    'source': article.get('source', {}).get('name', 'Unknown'),
                    'published': published_time or datetime.now(),
                    'url': article.get('url', ''),
                    'author': article.get('author', '')
                })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка парсинга статьи: {e}")
                continue
        
        logger.info(f"✅ Получено {len(articles)} новостей из NewsAPI для запроса '{query}'")
        return articles
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к NewsAPI: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при получении новостей: {e}")
        return []


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
        logger.info(f"🔍 Поиск новостей: {query}")
        news = fetch_newsapi_articles(api_key, query, days_back=2)
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


def save_news_to_db(news_items: List[Dict], ticker: str = 'MACRO', event_type: str = 'NEWS'):
    """
    Сохраняет новости из NewsAPI в БД
    
    Args:
        news_items: Список новостей
        ticker: Тикер для сохранения (по умолчанию MACRO)
        event_type: Тип события
    """
    if not news_items:
        return
    
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
    
    logger.info(f"✅ Сохранено {saved_count} новостей из NewsAPI, пропущено дубликатов: {skipped_count}")
    engine.dispose()


def fetch_and_save_newsapi_news():
    """
    Главная функция: получает новости из NewsAPI и сохраняет в БД
    """
    api_key = get_api_key()
    if not api_key:
        logger.warning("⚠️ NEWSAPI_KEY не настроен в config.env, пропускаем NewsAPI")
        return
    
    logger.info("🚀 Начало получения новостей из NewsAPI")
    
    # Получаем макро-новости
    macro_news = fetch_macro_news(api_key)
    if macro_news:
        save_news_to_db(macro_news, ticker='MACRO', event_type='MACRO_NEWS')
    
    logger.info("✅ Завершено получение новостей из NewsAPI")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_newsapi_news()

"""
Модуль для получения новостей из RSS фидов центральных банков
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import feedparser
import logging
import re
from datetime import datetime
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text

from config_loader import get_database_url
from services.http_outbound import outbound_session

logger = logging.getLogger(__name__)


# RSS фиды центральных банков
RSS_FEEDS = {
    'FOMC_STATEMENT': {
        'url': 'https://www.federalreserve.gov/feeds/press_all.xml',
        'region': 'USA',
        'event_type': 'FOMC_STATEMENT',
        'importance': 'HIGH'
    },
    'FOMC_SPEECH': {
        # Официальный URL: https://www.federalreserve.gov/feeds/feeds.htm (All Speeches)
        # Не press_speeches.xml — тот URL возвращает 404
        'url': 'https://www.federalreserve.gov/feeds/speeches.xml',
        'region': 'USA',
        'event_type': 'FOMC_SPEECH',
        'importance': 'HIGH'
    },
    'FOMC_MONETARY': {
        'url': 'https://www.federalreserve.gov/feeds/press_monetary.xml',
        'region': 'USA',
        'event_type': 'FOMC_STATEMENT',
        'importance': 'HIGH'
    },
    'BOE_STATEMENT': {
        # /rss — это HTML-страница со списком фидов; используем реальный RSS news
        'url': 'https://www.bankofengland.co.uk/rss/news',
        'region': 'UK',
        'event_type': 'BOE_STATEMENT',
        'importance': 'HIGH'
    },
    'ECB_STATEMENT': {
        'url': 'https://www.ecb.europa.eu/rss/press.html',
        'region': 'EU',
        'event_type': 'ECB_STATEMENT',
        'importance': 'HIGH'
    },
    'BOJ_STATEMENT': {
        'url': 'https://www.boj.or.jp/en/rss/whatsnew.xml',
        'region': 'Japan',
        'event_type': 'BOJ_STATEMENT',
        'importance': 'HIGH'
    }
}


def _sanitize_xml_for_parser(text: str, aggressive: bool = False) -> str:
    """
    Очистка XML для парсера. Если aggressive=True (для проблемных фидов вроде BOE),
    дополнительно заменяем символы, которые часто ломают парсинг.
    """
    # Убираем неверную декларацию кодировки
    text = re.sub(r'encoding\s*=\s*["\']us-ascii["\']', 'encoding="utf-8"', text, flags=re.I)
    # Убираем невалидные для XML 1.0 символы (control chars + C1 range)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    # Исправляем только «голый» & (не трогаем &amp; &#123; &#x1F; и т.д.)
    text = re.sub(r'&(?!(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);)', '&amp;', text)
    text = re.sub(r'&#0+;', '', text)
    if aggressive:
        # Для BOE и подобных: символы, которые часто дают "invalid token"
        text = text.replace('\u2018', "'").replace('\u2019', "'")  # smart quotes
        text = text.replace('\u201c', '"').replace('\u201d', '"')
        text = text.replace('\u2013', '-').replace('\u2014', '-')  # en/em dash
        text = text.replace('\u00a0', ' ')  # nbsp
    return text


def _fetch_rss_content(url: str, aggressive_sanitize: bool = False) -> Optional[str]:
    """
    Загружает RSS по URL с исправлением кодировки (многие фиды объявляют
    us-ascii, но отдают utf-8 — из-за этого падает парсер).
    aggressive_sanitize: для проблемных фидов (BOE) — дополнительная очистка.
    """
    try:
        sess = outbound_session("RSS_USE_SYSTEM_PROXY")
        r = sess.get(url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; LSE-NewsBot/1.0)',
            'Accept': 'application/rss+xml, application/xml, text/xml',
        })
        r.raise_for_status()
        raw = r.content
        try:
            text = raw.decode('utf-8', errors='replace')
        except Exception:
            text = raw.decode('cp1252', errors='replace')
        text = _sanitize_xml_for_parser(text, aggressive=aggressive_sanitize)
        return text
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить {url}: {e}")
        return None


def parse_rss_feed(feed_config: Dict) -> List[Dict]:
    """
    Парсит RSS фид и возвращает список новостей
    
    Args:
        feed_config: Конфигурация фида (url, region, event_type, importance)
        
    Returns:
        Список словарей с новостями
    """
    url = feed_config['url']
    region = feed_config['region']
    event_type = feed_config['event_type']
    importance = feed_config['importance']
    
    try:
        # BOE фид часто содержит символы, ломающие XML — включаем жёсткую санитизацию
        aggressive = 'bankofengland' in url.lower()
        content = _fetch_rss_content(url, aggressive_sanitize=aggressive)
        if not content:
            return []
        feed = feedparser.parse(content)
        
        if feed.bozo:
            # Если есть bozo, но есть entries - все равно используем их
            if not feed.entries:
                logger.warning(f"⚠️ Ошибка парсинга RSS фида {url}: {feed.bozo_exception}")
                return []
            else:
                logger.warning(f"⚠️ Предупреждение при парсинге {url}: {feed.bozo_exception}, но найдено {len(feed.entries)} записей")
        
        items = []
        for entry in feed.entries:
            # Парсим дату публикации
            published_time = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published_time = datetime(*entry.published_parsed[:6])
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                published_time = datetime(*entry.updated_parsed[:6])
            else:
                published_time = datetime.now()
            
            # Формируем контент
            content = entry.summary if hasattr(entry, 'summary') else entry.title
            if hasattr(entry, 'content') and entry.content:
                # Если есть content, используем его
                if isinstance(entry.content, list) and len(entry.content) > 0:
                    content = entry.content[0].get('value', content)
            
            item = {
                'title': entry.title,
                'link': entry.link if hasattr(entry, 'link') else '',
                'content': content,
                'published': published_time,
                'ticker': 'US_MACRO' if region == 'USA' else 'MACRO',
                'source': f"{region} Central Bank",
                'event_type': event_type,
                'region': region,
                'importance': importance
            }
            items.append(item)
        
        logger.info(f"✅ Получено {len(items)} новостей из {event_type}")
        return items
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении RSS фида {url}: {e}")
        return []


def fetch_all_rss_feeds() -> List[Dict]:
    """
    Получает новости из всех RSS фидов
    
    Returns:
        Список всех новостей
    """
    all_news = []
    
    for feed_name, feed_config in RSS_FEEDS.items():
        logger.info(f"📡 Получение новостей из {feed_name}...")
        try:
            news = parse_rss_feed(feed_config)
            all_news.extend(news)
        except Exception as e:
            logger.error(f"❌ Ошибка при получении {feed_name}: {e}")
    
    logger.info(f"✅ Всего получено {len(all_news)} новостей из RSS фидов")
    return all_news


def save_news_to_db(news_items: List[Dict], check_duplicates: bool = True) -> tuple:
    """
    Сохраняет новости в базу данных.

    Returns:
        (saved_count, skipped_count)
    """
    if not news_items:
        logger.info("ℹ️ Нет новостей для сохранения")
        return (0, 0)

    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    skipped_count = 0
    
    with engine.begin() as conn:
        for item in news_items:
            try:
                # Проверка дубликатов по link (если включена и link есть)
                if check_duplicates and item.get('link'):
                    existing = conn.execute(
                        text("""
                            SELECT id FROM knowledge_base 
                            WHERE link = :link
                        """),
                        {"link": item['link']}
                    ).fetchone()
                    
                    if existing:
                        skipped_count += 1
                        continue
                
                # Вставляем новость (ts = дата публикации в RSS; ingested_at = момент загрузки в БД)
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base 
                        (ts, ticker, source, content, event_type, region, importance, link, ingested_at)
                        VALUES (:ts, :ticker, :source, :content, :event_type, :region, :importance, :link, NOW())
                    """),
                    {
                        "ts": item['published'],
                        "ticker": item['ticker'],
                        "source": item['source'],
                        "content": f"{item['title']}\n\n{item['content']}\n\nLink: {item['link']}" if item.get('link') else f"{item['title']}\n\n{item['content']}",
                        "event_type": item.get('event_type'),
                        "region": item.get('region'),
                        "importance": item.get('importance'),
                        "link": item.get('link', '')
                    }
                )
                saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении новости '{item.get('title', '')[:50]}...': {e}")
    
    logger.info("✅ Сохранено %s новостей, пропущено дубликатов (тот же link в knowledge_base): %s", saved_count, skipped_count)
    if saved_count == 0 and skipped_count > 0:
        logger.info(
            "ℹ️ RSS: загрузка фидов сработала; новых вставок нет, потому что все URL уже сохранены ранее — так и должно быть при повторном кроне."
        )
    engine.dispose()
    return (saved_count, skipped_count)


def fetch_and_save_rss_news() -> tuple:
    """
    Главная функция: получает новости из RSS и сохраняет в БД.

    Returns:
        (saved_count, skipped_count)
    """
    logger.info("🚀 Начало получения новостей из RSS фидов центральных банков")
    news_items = fetch_all_rss_feeds()
    logger.info(
        "ℹ️ В ленте сейчас %s статей (полный снимок фидов). В БД попадут только новые URL; "
        "повторный крон почти всегда даст «сохранено 0» — это дедупликация по link, а не сбой сети.",
        len(news_items),
    )
    if not news_items:
        logger.info("✅ Завершено: из фидов получено 0 записей")
        return (0, 0)
    saved, skipped = save_news_to_db(news_items)
    logger.info("✅ Завершено получение новостей из RSS фидов")
    return (saved, skipped)


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_rss_news()

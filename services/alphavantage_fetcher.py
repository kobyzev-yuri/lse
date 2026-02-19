"""
Модуль для получения данных через Alpha Vantage API
- Earnings Calendar
- News Sentiment
"""

import requests
import csv
from io import StringIO
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value

logger = logging.getLogger(__name__)


def get_api_key() -> Optional[str]:
    """Получает API ключ Alpha Vantage из конфига"""
    return get_config_value('ALPHAVANTAGE_KEY', None)


def fetch_earnings_calendar(api_key: str, symbol: str = None) -> List[Dict]:
    """
    Получает календарь earnings через Alpha Vantage
    
    Args:
        api_key: API ключ Alpha Vantage
        symbol: Тикер (опционально, если None - все)
        
    Returns:
        Список словарей с данными earnings
    """
    url = "https://www.alphavantage.co/query"
    params = {
        'function': 'EARNINGS_CALENDAR',
        'apikey': api_key
    }
    if symbol:
        params['symbol'] = symbol
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        # Alpha Vantage возвращает CSV
        csv_data = response.text
        
        if not csv_data or 'Error' in csv_data:
            logger.warning(f"⚠️ Alpha Vantage вернул ошибку: {csv_data[:200]}")
            return []
        
        reader = csv.DictReader(StringIO(csv_data))
        
        earnings = []
        for row in reader:
            try:
                # Парсим дату
                report_date = None
                if row.get('reportDate'):
                    try:
                        report_date = datetime.strptime(row['reportDate'], '%Y-%m-%d')
                    except:
                        pass
                
                earnings.append({
                    'symbol': row.get('symbol', '').upper(),
                    'reportDate': report_date,
                    'estimate': float(row['estimate']) if row.get('estimate') and row['estimate'] != 'None' else None,
                    'currency': row.get('currency', 'USD')
                })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка парсинга строки earnings: {e}")
                continue
        
        logger.info(f"✅ Получено {len(earnings)} записей earnings из Alpha Vantage")
        return earnings
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Alpha Vantage: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при получении earnings: {e}")
        return []


def fetch_news_sentiment(api_key: str, tickers: str) -> List[Dict]:
    """
    Получает новости и sentiment через Alpha Vantage
    
    Args:
        api_key: API ключ Alpha Vantage
        tickers: Список тикеров через запятую (например, "MSFT,AAPL")
        
    Returns:
        Список словарей с новостями и sentiment
    """
    url = "https://www.alphavantage.co/query"
    params = {
        'function': 'NEWS_SENTIMENT',
        'tickers': tickers,
        'apikey': api_key,
        'limit': 50  # Максимум для бесплатного tier
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        if 'Error Message' in data:
            logger.error(f"❌ Alpha Vantage ошибка: {data['Error Message']}")
            return []
        
        if 'Note' in data:
            logger.warning(f"⚠️ Alpha Vantage лимит: {data['Note']}")
            return []
        
        news_items = []
        for item in data.get('feed', []):
            try:
                # Парсим дату
                published_time = None
                if item.get('time_published'):
                    try:
                        # Формат: 20240219T120000
                        time_str = item['time_published']
                        published_time = datetime.strptime(time_str, '%Y%m%dT%H%M%S')
                    except:
                        pass
                
                # Извлекаем тикеры из новости
                ticker_symbols = []
                if item.get('ticker_sentiment'):
                    ticker_symbols = [t['ticker'] for t in item['ticker_sentiment']]
                
                news_items.append({
                    'title': item.get('title', ''),
                    'content': item.get('summary', ''),
                    'source': item.get('source', ''),
                    'published': published_time or datetime.now(),
                    'url': item.get('url', ''),
                    'tickers': ticker_symbols,
                    'overall_sentiment': item.get('overall_sentiment_score', 0.0),
                    'ticker_sentiment': item.get('ticker_sentiment', [])
                })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка парсинга новости: {e}")
                continue
        
        logger.info(f"✅ Получено {len(news_items)} новостей из Alpha Vantage")
        return news_items
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Alpha Vantage: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при получении новостей: {e}")
        return []


def save_earnings_to_db(earnings: List[Dict]):
    """
    Сохраняет earnings в базу данных
    
    Args:
        earnings: Список earnings для сохранения
    """
    if not earnings:
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    
    with engine.begin() as conn:
        for earning in earnings:
            try:
                if not earning.get('symbol') or not earning.get('reportDate'):
                    continue
                
                # Формируем контент
                content = f"Earnings report for {earning['symbol']}"
                if earning.get('estimate'):
                    content += f"\nEstimate: {earning['estimate']} {earning.get('currency', 'USD')}"
                
                # Проверяем дубликаты
                existing = conn.execute(
                    text("""
                        SELECT id FROM knowledge_base 
                        WHERE ticker = :ticker 
                        AND event_type = 'EARNINGS'
                        AND DATE(ts) = DATE(:report_date)
                    """),
                    {
                        "ticker": earning['symbol'],
                        "report_date": earning['reportDate']
                    }
                ).fetchone()
                
                if existing:
                    continue
                
                # Вставляем
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base 
                        (ts, ticker, source, content, event_type, importance)
                        VALUES (:ts, :ticker, :source, :content, :event_type, :importance)
                    """),
                    {
                        "ts": earning['reportDate'],
                        "ticker": earning['symbol'],
                        "source": "Alpha Vantage Earnings Calendar",
                        "content": content,
                        "event_type": "EARNINGS",
                        "importance": "HIGH"
                    }
                )
                saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении earnings для {earning.get('symbol')}: {e}")
    
    logger.info(f"✅ Сохранено {saved_count} earnings в БД")
    engine.dispose()


def save_news_to_db(news_items: List[Dict]):
    """
    Сохраняет новости из Alpha Vantage в БД
    
    Args:
        news_items: Список новостей
    """
    if not news_items:
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    
    with engine.begin() as conn:
        for item in news_items:
            try:
                # Сохраняем для каждого тикера отдельно
                tickers = item.get('tickers', [])
                if not tickers:
                    tickers = ['MACRO']  # Если тикеров нет, сохраняем как макро
                
                for ticker in tickers:
                    # Проверяем дубликаты по URL
                    if item.get('url'):
                        existing = conn.execute(
                            text("""
                                SELECT id FROM knowledge_base 
                                WHERE link = :url AND ticker = :ticker
                            """),
                            {"url": item['url'], "ticker": ticker}
                        ).fetchone()
                        
                        if existing:
                            continue
                    
                    # Получаем sentiment для этого тикера
                    ticker_sentiment = None
                    if item.get('ticker_sentiment'):
                        for ts in item['ticker_sentiment']:
                            if ts.get('ticker') == ticker:
                                ticker_sentiment = float(ts.get('relevance_score', 0.0)) * float(ts.get('ticker_sentiment_score', 0.5))
                                break
                    
                    # Если нет sentiment для тикера, используем общий
                    if ticker_sentiment is None:
                        ticker_sentiment = float(item.get('overall_sentiment', 0.5))
                    
                    # Нормализуем sentiment от -1.0 до 1.0 в диапазон 0.0-1.0
                    sentiment_score = (ticker_sentiment + 1.0) / 2.0
                    
                    conn.execute(
                        text("""
                            INSERT INTO knowledge_base 
                            (ts, ticker, source, content, sentiment_score, link, event_type)
                            VALUES (:ts, :ticker, :source, :content, :sentiment_score, :link, :event_type)
                        """),
                        {
                            "ts": item['published'],
                            "ticker": ticker,
                            "source": item.get('source', 'Alpha Vantage'),
                            "content": f"{item.get('title', '')}\n\n{item.get('content', '')}",
                            "sentiment_score": sentiment_score,
                            "link": item.get('url', ''),
                            "event_type": "NEWS"
                        }
                    )
                    saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении новости: {e}")
    
    logger.info(f"✅ Сохранено {saved_count} новостей из Alpha Vantage в БД")
    engine.dispose()


def fetch_and_save_alphavantage_data(tickers: List[str] = None):
    """
    Главная функция: получает данные из Alpha Vantage и сохраняет в БД
    
    Args:
        tickers: Список тикеров для отслеживания (если None - использует из конфига)
    """
    api_key = get_api_key()
    if not api_key:
        logger.warning("⚠️ ALPHAVANTAGE_KEY не настроен в config.env, пропускаем Alpha Vantage")
        return
    
    logger.info("🚀 Начало получения данных из Alpha Vantage")
    
    # Получаем earnings calendar
    logger.info("📅 Получение Earnings Calendar...")
    earnings = fetch_earnings_calendar(api_key)
    if earnings:
        save_earnings_to_db(earnings)
    
    # Получаем новости (если указаны тикеры)
    if tickers:
        tickers_str = ','.join(tickers[:5])  # Alpha Vantage ограничивает количество тикеров
        logger.info(f"📰 Получение новостей для тикеров: {tickers_str}...")
        news = fetch_news_sentiment(api_key, tickers_str)
        if news:
            save_news_to_db(news)
    
    logger.info("✅ Завершено получение данных из Alpha Vantage")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # Пример использования
    fetch_and_save_alphavantage_data(['MSFT', 'AAPL', 'GOOGL'])

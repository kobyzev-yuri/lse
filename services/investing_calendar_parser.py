"""
Модуль для парсинга экономического календаря Investing.com
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import requests
from bs4 import BeautifulSoup
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text
import time

from config_loader import get_database_url

logger = logging.getLogger(__name__)


# Регионы для экономического календаря
REGIONS = {
    'USA': {'code': '5', 'name': 'United States'},
    'UK': {'code': '6', 'name': 'United Kingdom'},
    'EU': {'code': '17', 'name': 'Eurozone'},
    'Japan': {'code': '35', 'name': 'Japan'},
    'China': {'code': '37', 'name': 'China'},
    'Switzerland': {'code': '39', 'name': 'Switzerland'}
}


def fetch_investing_calendar(region: str, days_ahead: int = 7) -> List[Dict]:
    """
    Парсит экономический календарь Investing.com
    
    Args:
        region: Код региона (USA, UK, EU, Japan, China, Switzerland)
        days_ahead: Количество дней вперед
        
    Returns:
        Список словарей с событиями
    """
    if region not in REGIONS:
        logger.warning(f"⚠️ Неизвестный регион: {region}")
        return []
    
    region_code = REGIONS[region]['code']
    url = "https://www.investing.com/economic-calendar/"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    try:
        # Параметры для фильтрации
        params = {
            'timeZone': '8',  # UTC
            'timeFilter': 'timeRemain',
            'currentTab': 'today'
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Ищем таблицу с событиями
        # Структура может меняться, поэтому используем гибкий поиск
        table = soup.find('table', {'id': 'economicCalendarData'})
        if not table:
            # Пробуем альтернативные селекторы
            table = soup.find('table', class_='genTbl')
        
        if not table:
            logger.warning(f"⚠️ Не найдена таблица календаря для {region}")
            return []
        
        events = []
        rows = table.find_all('tr')[1:]  # Пропускаем заголовок
        
        for row in rows:
            try:
                cols = row.find_all('td')
                if len(cols) < 4:
                    continue
                
                # Парсим колонки (структура может варьироваться)
                time_str = cols[0].get_text(strip=True) if len(cols) > 0 else ''
                currency = cols[1].get_text(strip=True) if len(cols) > 1 else ''
                
                # Важность (иконка)
                importance = 'MEDIUM'
                importance_elem = cols[2] if len(cols) > 2 else None
                if importance_elem:
                    importance_class = importance_elem.get('class', [])
                    if 'high' in str(importance_class).lower() or 'bull' in str(importance_class).lower():
                        importance = 'HIGH'
                    elif 'low' in str(importance_class).lower():
                        importance = 'LOW'
                
                event_name = cols[3].get_text(strip=True) if len(cols) > 3 else ''
                
                # Фактическое значение (если есть)
                actual = cols[4].get_text(strip=True) if len(cols) > 4 else None
                forecast = cols[5].get_text(strip=True) if len(cols) > 5 else None
                previous = cols[6].get_text(strip=True) if len(cols) > 6 else None
                
                # Определяем тип события
                event_type = 'ECONOMIC_INDICATOR'
                event_lower = event_name.lower()
                if 'rate' in event_lower and 'decision' in event_lower:
                    event_type = 'RATE_DECISION'
                elif 'cpi' in event_lower or 'inflation' in event_lower:
                    event_type = 'CPI'
                elif 'ppi' in event_lower:
                    event_type = 'PPI'
                elif 'nfp' in event_lower or 'non-farm payrolls' in event_lower:
                    event_type = 'NFP'
                elif 'pmi' in event_lower:
                    event_type = 'PMI'
                elif 'gdp' in event_lower:
                    event_type = 'GDP'
                elif 'unemployment' in event_lower:
                    event_type = 'UNEMPLOYMENT'
                elif 'retail sales' in event_lower:
                    event_type = 'RETAIL_SALES'
                
                # Парсим дату события (сегодня + время или будущая дата)
                event_date = datetime.now()
                if time_str:
                    try:
                        # Пробуем парсить время (формат может быть разный)
                        if ':' in time_str:
                            hour, minute = map(int, time_str.split(':'))
                            event_date = event_date.replace(hour=hour, minute=minute, second=0)
                    except:
                        pass
                
                event = {
                    'time': time_str,
                    'currency': currency,
                    'importance': importance,
                    'event': event_name,
                    'actual': actual,
                    'forecast': forecast,
                    'previous': previous,
                    'region': region,
                    'event_type': event_type,
                    'event_date': event_date
                }
                events.append(event)
                
            except Exception as e:
                logger.warning(f"⚠️ Ошибка парсинга строки события: {e}")
                continue
        
        logger.info(f"✅ Получено {len(events)} событий из Investing.com для {region}")
        return events
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Investing.com: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при парсинге календаря: {e}")
        return []


def fetch_all_regions_calendar() -> List[Dict]:
    """
    Получает календарь для всех регионов
    
    Returns:
        Список всех событий
    """
    all_events = []
    
    for region in REGIONS.keys():
        logger.info(f"📅 Получение календаря для {region}...")
        events = fetch_investing_calendar(region)
        all_events.extend(events)
        
        # Небольшая задержка между запросами
        time.sleep(2)
    
    logger.info(f"✅ Всего получено {len(all_events)} событий из Investing.com")
    return all_events


def save_events_to_db(events: List[Dict]):
    """
    Сохраняет события календаря в БД
    
    Args:
        events: Список событий
    """
    if not events:
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    skipped_count = 0
    
    with engine.begin() as conn:
        for event in events:
            try:
                # Формируем контент
                content = f"{event['event']}"
                if event.get('forecast'):
                    content += f"\nForecast: {event['forecast']}"
                if event.get('previous'):
                    content += f"\nPrevious: {event['previous']}"
                if event.get('actual'):
                    content += f"\nActual: {event['actual']}"
                
                # Определяем ticker
                ticker = 'US_MACRO' if event['region'] == 'USA' else 'MACRO'
                
                # Проверяем дубликаты (по событию, дате и региону)
                existing = conn.execute(
                    text("""
                        SELECT id FROM knowledge_base 
                        WHERE event_type = :event_type
                        AND region = :region
                        AND DATE(ts) = DATE(:event_date)
                        AND content LIKE :event_name
                    """),
                    {
                        "event_type": event['event_type'],
                        "region": event['region'],
                        "event_date": event['event_date'],
                        "event_name": f"%{event['event'][:50]}%"
                    }
                ).fetchone()
                
                if existing:
                    skipped_count += 1
                    continue
                
                # Вставляем событие
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base 
                        (ts, ticker, source, content, event_type, region, importance)
                        VALUES (:ts, :ticker, :source, :content, :event_type, :region, :importance)
                    """),
                    {
                        "ts": event['event_date'],
                        "ticker": ticker,
                        "source": f"Investing.com Economic Calendar ({event['region']})",
                        "content": content,
                        "event_type": event['event_type'],
                        "region": event['region'],
                        "importance": event['importance']
                    }
                )
                saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении события: {e}")
    
    logger.info(f"✅ Сохранено {saved_count} событий, пропущено дубликатов: {skipped_count}")
    engine.dispose()


def fetch_and_save_investing_calendar():
    """
    Главная функция: получает календарь из Investing.com и сохраняет в БД
    """
    logger.info("🚀 Начало получения экономического календаря из Investing.com")
    
    # Получаем события для всех регионов
    events = fetch_all_regions_calendar()
    
    # Сохраняем в БД
    if events:
        save_events_to_db(events)
    
    logger.info("✅ Завершено получение календаря из Investing.com")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_investing_calendar()

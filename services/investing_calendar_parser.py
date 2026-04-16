"""
Модуль для экономического календаря Investing.com.

По умолчанию — только JSON API (endpoints.investing.com, как nyse/sources/ecalendar.py), без
скрытого fallback на HTML. Обход HTML по регионам только при INVESTING_CALENDAR_USE_HTML=true
(legacy: таблица может подгружаться через JS; если таблица не найдена — пусто).

Макро-события: в cron по умолчанию экономические индикаторы Alpha Vantage
выключены (ALPHAVANTAGE_FETCH_ECONOMIC=false). Даже при включении бесплатный
план часто возвращает пустой ответ (премиум-эндпоинты). Полноценное «дублирование»
макро через Alpha Vantage возможно только при платной подписке и явном
включении ALPHAVANTAGE_FETCH_ECONOMIC=true в config.env.
"""

import os
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

from config_loader import get_config_value, get_database_url
from services.http_outbound import outbound_session
from services.kb_extended_fields import (
    investing_calendar_external_id,
    investing_calendar_raw_payload,
    kb_content_sha256,
)

logger = logging.getLogger(__name__)

# Сохранять HTML при ненайденной таблице для отладки (INVESTING_CALENDAR_DEBUG_HTML=1)
DEBUG_SAVE_HTML = os.environ.get('INVESTING_CALENDAR_DEBUG_HTML', '').strip().lower() in ('1', 'true', 'yes')

# Защита от 429 Too Many Requests (Investing.com)
INVESTING_CALENDAR_429_BACKOFF = [45, 90]
INVESTING_CALENDAR_429_MAX_RETRIES = 2
_CALENDAR_RATE_LIMIT_HIT = False

# Регионы для экономического календаря
REGIONS = {
    'USA': {'code': '5', 'name': 'United States'},
    'UK': {'code': '6', 'name': 'United Kingdom'},
    'EU': {'code': '17', 'name': 'Eurozone'},
    'Japan': {'code': '35', 'name': 'Japan'},
    'China': {'code': '37', 'name': 'China'},
    'Switzerland': {'code': '39', 'name': 'Switzerland'}
}


def _find_calendar_table(soup: BeautifulSoup, region: str):
    """Ищет таблицу календаря по разным селекторам (структура сайта может меняться)."""
    # По ID и классам (исторические и возможные новые)
    by_id = soup.find('table', id='economicCalendarData')
    if by_id:
        logger.info(f"✅ Найдена таблица календаря для {region} (id=economicCalendarData)")
        return by_id

    for tid in ('economicCalendar', 'ec-table', 'economic-calendar-table'):
        t = soup.find('table', id=tid)
        if t:
            logger.info(f"✅ Найдена таблица календаря для {region} (id={tid})")
            return t

    # По классам (class может быть списком или строкой)
    for cls in ('genTbl', 'js-ec-table', 'economic-calendar-table', 'calendar-table'):
        def has_class(c):
            if not c:
                return False
            parts = c if isinstance(c, list) else [c]
            return any(cls in str(p) for p in parts)
        t = soup.find('table', class_=has_class)
        if t:
            logger.info(f"✅ Найдена таблица календаря для {region} (class~{cls})")
            return t

    # Любая таблица с классом, содержащим 'calendar' или 'economic'
    for t in soup.find_all('table'):
        classes = t.get('class') or []
        if isinstance(classes, str):
            classes = [classes]
        if any('calendar' in str(c).lower() or 'economic' in str(c).lower() for c in classes):
            logger.info(f"✅ Найдена таблица календаря для {region} (по классу)")
            return t

    # Fallback: таблица с подходящей структурой (много строк, несколько колонок)
    for t in soup.find_all('table'):
        rows = t.find_all('tr')
        if len(rows) < 3:
            continue
        first_data_row = next((r for r in rows if r.find_all('td')), None)
        if not first_data_row:
            continue
        cols = len(first_data_row.find_all('td'))
        if cols >= 4:
            logger.info(f"✅ Найдена таблица календаря для {region} (fallback: таблица {len(rows)}x{cols})")
            return t

    return None


def _debug_save_html(soup: BeautifulSoup, region: str) -> None:
    """Сохраняет HTML в /tmp для отладки (если INVESTING_CALENDAR_DEBUG_HTML=1)."""
    try:
        path = Path('/tmp') / f'investing_calendar_{region}.html'
        path.write_text(str(soup), encoding='utf-8')
        logger.info(f"📁 HTML сохранён для отладки: {path}")
    except Exception as e:
        logger.warning(f"Не удалось сохранить HTML: {e}")


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
    
    global _CALENDAR_RATE_LIMIT_HIT
    try:
        # Параметры для фильтрации
        params = {
            'timeZone': '8',  # UTC
            'timeFilter': 'timeRemain',
            'currentTab': 'today'
        }
        
        sess = outbound_session("INVESTING_CALENDAR_USE_SYSTEM_PROXY")
        response = None
        for attempt in range(INVESTING_CALENDAR_429_MAX_RETRIES + 1):
            response = sess.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 429:
                if attempt < INVESTING_CALENDAR_429_MAX_RETRIES:
                    wait = INVESTING_CALENDAR_429_BACKOFF[attempt]
                    logger.warning(
                        "Investing.com calendar: 429 Too Many Requests, ждём %s с перед повтором (регион=%s, попытка %s)",
                        wait, region, attempt + 1,
                    )
                    time.sleep(wait)
                    continue
                _CALENDAR_RATE_LIMIT_HIT = True
                logger.warning(
                    "Investing.com calendar: 429 после %s попыток (регион=%s). Останавливаем опрос регионов до следующего запуска.",
                    INVESTING_CALENDAR_429_MAX_RETRIES + 1,
                    region,
                )
                return []
            response.raise_for_status()
            break
        if response is None:
            return []
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Ищем таблицу с событиями (страница может подгружать данные через JS)
        table = _find_calendar_table(soup, region)
        
        if not table:
            logger.warning(
                f"⚠️ Не найдена таблица календаря для {region}. "
                "HTML структура могла измениться или данные подгружаются через JS."
            )
            if DEBUG_SAVE_HTML:
                _debug_save_html(soup, region)
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
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 429:
            _CALENDAR_RATE_LIMIT_HIT = True
            logger.warning(
                "Investing.com calendar: 429 (регион=%s). Прерываем опрос календаря до следующего запуска.",
                region,
            )
            return []
        logger.error(f"❌ Ошибка запроса к Investing.com: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при парсинге календаря: {e}")
        return []


def fetch_all_regions_calendar() -> List[Dict]:
    """
    Получает календарь для всех регионов.

    По умолчанию — только JSON API (как nyse), без fallback на HTML.
    INVESTING_CALENDAR_USE_HTML=true — только legacy-обход HTML по регионам.
    """
    global _CALENDAR_RATE_LIMIT_HIT
    _CALENDAR_RATE_LIMIT_HIT = False

    use_html_only = (
        str(get_config_value("INVESTING_CALENDAR_USE_HTML") or "")
        .strip()
        .lower()
        in ("1", "true", "yes")
    )
    if not use_html_only:
        try:
            from services.investing_calendar_api import fetch_investing_calendar_api_events

            api_events = fetch_investing_calendar_api_events(
                days_back=2, days_forward=7, limit=500
            )
            logger.info(
                "✅ Investing.com calendar: JSON API, %s событий",
                len(api_events),
            )
            return api_events
        except Exception as e:
            logger.error(
                "Investing.com calendar: ошибка JSON API (%s). HTML не используется; "
                "для legacy-скрапинга задайте INVESTING_CALENDAR_USE_HTML=true.",
                e,
            )
            return []

    all_events = []

    for region in REGIONS.keys():
        if _CALENDAR_RATE_LIMIT_HIT:
            logger.warning(
                "Investing.com calendar: дальнейший опрос регионов пропущен (получен 429 в этом запуске)."
            )
            break
        logger.info(f"📅 Получение календаря для {region}...")
        events = fetch_investing_calendar(region)
        all_events.extend(events)
        
        # Увеличенная задержка между запросами (чтобы избежать 429 Too Many Requests)
        if region != list(REGIONS.keys())[-1]:  # Не ждем после последнего региона
            time.sleep(5)  # Увеличено с 2 до 5 секунд
    
    logger.info(f"✅ Всего получено {len(all_events)} событий из Investing.com")
    return all_events


def _is_calendar_content_worth_saving(content: str, event_name: str) -> bool:
    """
    Решает, имеет ли смысл сохранять запись календаря в knowledge_base.
    Не сохраняем «шум»: только число (19.60M), без названия события или без текста.
    """
    if not content or not content.strip():
        return False
    text = content.strip()
    if len(text) < 25:
        return False
    if not event_name or len(event_name.strip()) < 3:
        return False
    if " " not in text:
        return False
    return True


def save_events_to_db(events: List[Dict]):
    """
    Сохраняет события календаря в БД.
    Записи без осмысленного текста (только число вроде 19.60M) не сохраняются.
    """
    if not events:
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    skipped_count = 0
    skipped_noise = 0
    
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
                
                if not _is_calendar_content_worth_saving(content, event.get('event') or ''):
                    skipped_noise += 1
                    continue
                
                # Определяем ticker / symbol (макро: US_MACRO vs MACRO)
                ticker = 'US_MACRO' if event['region'] == 'USA' else 'MACRO'
                symbol = ticker
                ext_id = investing_calendar_external_id(
                    event["region"],
                    event["event_date"],
                    event.get("event") or "",
                    event["event_type"],
                )
                content_sha = kb_content_sha256(content)
                raw_json = investing_calendar_raw_payload(event)

                # Вставка с расширенной схемой; дедуп по partial UNIQUE(external_id)
                result = conn.execute(
                    text("""
                        INSERT INTO knowledge_base
                        (ts, ticker, source, content, event_type, region, importance, link, ingested_at,
                         exchange, symbol, external_id, content_sha256, raw_payload)
                        VALUES
                        (:ts, :ticker, :source, :content, :event_type, :region, :importance, NULL, NOW(),
                         NULL, :symbol, :external_id, :content_sha256, CAST(:raw_payload AS jsonb))
                        ON CONFLICT DO NOTHING
                    """),
                    {
                        "ts": event['event_date'],
                        "ticker": ticker,
                        "source": f"Investing.com Economic Calendar ({event['region']})",
                        "content": content,
                        "event_type": event['event_type'],
                        "region": event['region'],
                        "importance": event['importance'],
                        "symbol": symbol,
                        "external_id": ext_id[:512],
                        "content_sha256": content_sha,
                        "raw_payload": raw_json,
                    },
                )
                if result.rowcount and result.rowcount > 0:
                    saved_count += 1
                else:
                    skipped_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении события: {e}")
    
    logger.info(
        f"✅ Сохранено {saved_count} событий, пропущено дубликатов: {skipped_count}, "
        f"пропущено без текста: {skipped_noise}"
    )
    engine.dispose()


def fetch_and_save_investing_calendar():
    """
    Главная функция: получает календарь из Investing.com и сохраняет в БД.
    Источник: JSON API (по умолчанию, как nyse) или только при флаге — HTML (см. fetch_all_regions_calendar).
    Макро-события через Alpha Vantage: опционально (ALPHAVANTAGE_FETCH_ECONOMIC=true)
    и на бесплатном плане часто недоступны.
    """
    logger.info("🚀 Начало получения экономического календаря из Investing.com")
    
    events = fetch_all_regions_calendar()
    
    if events:
        save_events_to_db(events)
        logger.info("✅ Завершено получение календаря из Investing.com")
    else:
        logger.info(
            "Календарь Investing.com: событий нет (JSON API вернуло пусто или была ошибка — см. лог выше; "
            "legacy HTML только при INVESTING_CALENDAR_USE_HTML=true). "
            "Доп. макро: ALPHAVANTAGE_FETCH_ECONOMIC=true (на free AV часто пусто)."
        )


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    fetch_and_save_investing_calendar()

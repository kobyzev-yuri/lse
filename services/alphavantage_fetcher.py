"""
Модуль для получения данных через Alpha Vantage API
- Earnings Calendar
- News Sentiment
- Economic Indicators (CPI, GDP, FEDERAL_FUNDS_RATE, TREASURY_YIELD, UNEMPLOYMENT)
- Technical Indicators (RSI, MACD, BBANDS, ADX, STOCH)
"""

import os
import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import requests
import csv
import time
from io import StringIO
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import create_engine, text

from config_loader import get_database_url, get_config_value

logger = logging.getLogger(__name__)

# Таймаут и повторы для Alpha Vantage (часто даёт Read timed out)
AV_REQUEST_TIMEOUT = int(os.environ.get('ALPHAVANTAGE_TIMEOUT', '90'))
AV_MAX_RETRIES = int(os.environ.get('ALPHAVANTAGE_MAX_RETRIES', '3'))
AV_RETRY_DELAY = float(os.environ.get('ALPHAVANTAGE_RETRY_DELAY', '10'))


def _av_use_system_proxy() -> bool:
    """True — учитывать HTTP(S)_PROXY из окружения. По умолчанию False: иначе при socks:// без PySocks падает «Missing dependencies for SOCKS support»."""
    raw = (get_config_value("ALPHAVANTAGE_USE_SYSTEM_PROXY", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes")


def _get_with_retry(url: str, params: Dict, timeout: int = None) -> Optional[requests.Response]:
    """GET с повторными попытками при таймауте или 5xx."""
    timeout = timeout or AV_REQUEST_TIMEOUT
    last_error = None
    session = requests.Session()
    session.trust_env = _av_use_system_proxy()
    for attempt in range(AV_MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code >= 500 and attempt < AV_MAX_RETRIES:
                last_error = f"HTTP {response.status_code}"
                logger.warning(f"⚠️ Alpha Vantage {last_error}, повтор через {AV_RETRY_DELAY} с...")
                time.sleep(AV_RETRY_DELAY)
                continue
            return response
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt < AV_MAX_RETRIES:
                logger.warning(f"⚠️ Alpha Vantage таймаут/ошибка соединения, повтор через {AV_RETRY_DELAY} с...")
                time.sleep(AV_RETRY_DELAY)
            else:
                raise
        except requests.exceptions.RequestException as e:
            err = str(e).lower()
            if "socks" in err and session.trust_env and attempt == 0:
                logger.warning(
                    "Alpha Vantage: ошибка SOCKS/proxy (%s) — повтор без system proxy (pip install pysocks или ALPHAVANTAGE_USE_SYSTEM_PROXY=false)",
                    e,
                )
                session.trust_env = False
                time.sleep(0.5)
                continue
            raise
    return None


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
        response = _get_with_retry(url, params)
        if not response:
            return []
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
        response = _get_with_retry(url, params)
        if not response:
            return []
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
    skipped_count = 0
    error_count = 0
    
    try:
        from services.ticker_groups import get_tracked_tickers_for_kb, kb_ingest_tracked_tickers_only
        tracked = set(get_tracked_tickers_for_kb()) if kb_ingest_tracked_tickers_only() else None
    except Exception:
        tracked = None  # если модуль недоступен — сохраняем всех (как раньше)

    with engine.begin() as conn:
        for earning in earnings:
            try:
                if not earning.get('symbol') or not earning.get('reportDate'):
                    skipped_count += 1
                    continue
                if tracked is not None and earning['symbol'] not in tracked:
                    skipped_count += 1
                    continue

                # Формируем контент
                content = f"Earnings report for {earning['symbol']}"
                if earning.get('estimate'):
                    content += f"\nEstimate: {earning['estimate']} {earning.get('currency', 'USD')}"
                
                # Проверяем дубликаты (более гибкая проверка)
                existing = conn.execute(
                    text("""
                        SELECT id FROM knowledge_base 
                        WHERE ticker = :ticker 
                        AND event_type = 'EARNINGS'
                        AND DATE(ts) = DATE(:report_date)
                        AND source = 'Alpha Vantage Earnings Calendar'
                    """),
                    {
                        "ticker": earning['symbol'],
                        "report_date": earning['reportDate']
                    }
                ).fetchone()
                
                if existing:
                    skipped_count += 1
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
                error_count += 1
                logger.error(f"❌ Ошибка при сохранении earnings для {earning.get('symbol')}: {e}")
    
    logger.info(
        f"✅ Earnings: сохранено {saved_count}, пропущено дубликатов {skipped_count}, "
        f"ошибок {error_count} из {len(earnings)} полученных"
    )
    engine.dispose()


def save_news_to_db(news_items: List[Dict]):
    """
    Сохраняет новости из Alpha Vantage в БД.
    Фильтр по списку тикеров — только при KB_INGEST_TRACKED_TICKERS_ONLY=true.
    """
    if not news_items:
        return

    try:
        from services.ticker_groups import get_tracked_tickers_for_kb, kb_ingest_tracked_tickers_only
        tracked = set(get_tracked_tickers_for_kb()) if kb_ingest_tracked_tickers_only() else None
    except Exception:
        tracked = None

    db_url = get_database_url()
    engine = create_engine(db_url)

    saved_count = 0

    with engine.begin() as conn:
        for item in news_items:
            try:
                tickers = item.get('tickers', [])
                if not tickers:
                    tickers = ['MACRO']

                for ticker in tickers:
                    if tracked is not None and ticker not in tracked:
                        continue
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
                            (ts, ticker, source, content, sentiment_score, link, event_type, ingested_at)
                            VALUES (:ts, :ticker, :source, :content, :sentiment_score, :link, :event_type, NOW())
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


def fetch_economic_indicator(api_key: str, function: str, interval: str = None) -> List[Dict]:
    """
    Получает экономический индикатор через Alpha Vantage
    
    Args:
        api_key: API ключ Alpha Vantage
        function: Название функции (CPI, GDP, FEDERAL_FUNDS_RATE, TREASURY_YIELD, UNEMPLOYMENT, INFLATION)
        interval: Интервал (для некоторых индикаторов: monthly, quarterly, annual, daily, weekly)
        
    Returns:
        Список словарей с данными индикатора
    """
    url = "https://www.alphavantage.co/query"
    params = {
        'function': function,
        'apikey': api_key
    }
    
    if interval:
        params['interval'] = interval
    
    # Специфичные параметры для некоторых индикаторов
    if function == 'TREASURY_YIELD':
        if not interval:
            params['interval'] = 'monthly'
        params['maturity'] = '10year'  # По умолчанию 10-летние облигации
    
    try:
        response = _get_with_retry(url, params)
        if not response:
            return []
        response.raise_for_status()
        
        data = response.json()
        
        if 'Error Message' in data:
            logger.error(f"❌ Alpha Vantage ошибка для {function}: {data['Error Message']}")
            return []
        
        if 'Note' in data:
            logger.warning(f"⚠️ Alpha Vantage лимит для {function}: {data['Note']}")
            return []
        
        # Ответ только с ключом Information = лимит или премиум (часто у экономических индикаторов)
        if list(data.keys()) == ['Information'] or (len(data) == 1 and 'Information' in data):
            msg = (data.get('Information') or '')[:200]
            logger.warning(
                f"⚠️ Alpha Vantage для {function}: ответ без данных. "
                f"Information: {msg}. Возможно лимит бесплатного плана или премиум-эндпоинт."
            )
            if "25 requests per day" in msg or "rate limit" in msg.lower():
                logger.warning(
                    "💡 На бесплатном плане (25 запросов/день) в config.env установите "
                    "ALPHAVANTAGE_FETCH_ECONOMIC=false и ALPHAVANTAGE_FETCH_TECHNICAL=false, "
                    "чтобы тратить лимит только на Earnings и News."
                )
            return []
        
        # Извлекаем временной ряд
        time_series_key = None
        for key in data.keys():
            if 'Time Series' in key or (key.lower() == 'data' and isinstance(data.get(key), (dict, list))):
                time_series_key = key
                break
        
        if not time_series_key:
            # Некоторые индикаторы возвращают данные напрямую как список
            if 'data' in data:
                data_list = data['data']
                if isinstance(data_list, list):
                    # Обрабатываем список данных
                    indicators = []
                    for item in data_list:
                        if isinstance(item, dict):
                            date_str = item.get('date') or item.get('timestamp')
                            value = item.get('value') or item.get('close')
                            if date_str and value is not None:
                                try:
                                    # Пробуем разные форматы даты
                                    date_obj = None
                                    for fmt in ['%Y-%m-%d', '%Y-%m', '%Y']:
                                        try:
                                            date_obj = datetime.strptime(date_str, fmt)
                                            break
                                        except:
                                            continue
                                    if date_obj:
                                        indicators.append({
                                            'date': date_obj,
                                            'value': float(value),
                                            'indicator': function
                                        })
                                except Exception as e:
                                    logger.debug(f"Ошибка парсинга даты/значения для {function}: {e}")
                                    pass
                    if indicators:
                        logger.info(f"✅ Получено {len(indicators)} записей для {function} (из data списка)")
                        return indicators
                return data_list if isinstance(data_list, list) else []
            
            # Пробуем найти другие ключи с данными
            for key in ['data', 'values', 'series']:
                if key in data and isinstance(data[key], list):
                    logger.info(f"✅ Найдены данные для {function} в ключе '{key}' (список)")
                    # Обрабатываем список аналогично выше
                    indicators = []
                    for item in data[key]:
                        if isinstance(item, dict):
                            date_str = item.get('date') or item.get('timestamp')
                            value = item.get('value') or item.get('close')
                            if date_str and value is not None:
                                try:
                                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                                    indicators.append({
                                        'date': date_obj,
                                        'value': float(value),
                                        'indicator': function
                                    })
                                except:
                                    pass
                    if indicators:
                        return indicators
            
            # Логируем структуру ответа для отладки
            available_keys = list(data.keys())[:10]
            logger.warning(
                f"⚠️ Не найдена временная серия для {function}. "
                f"Доступные ключи: {available_keys}. "
                f"Попробуйте проверить документацию Alpha Vantage для {function}."
            )
            # Для отладки: логируем первые несколько символов ответа
            if len(str(data)) < 500:
                logger.debug(f"Структура ответа для {function}: {data}")
            return []
        
        time_series = data[time_series_key]
        
        # Проверяем тип: может быть список или словарь
        if isinstance(time_series, list):
            # Обрабатываем список
            indicators = []
            for item in time_series:
                if isinstance(item, dict):
                    date_str = item.get('date') or item.get('timestamp')
                    value = item.get('value') or item.get('close')
                    if date_str and value is not None:
                        try:
                            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                            indicators.append({
                                'date': date_obj,
                                'value': float(value),
                                'indicator': function
                            })
                        except:
                            pass
            if indicators:
                logger.info(f"✅ Получено {len(indicators)} записей для {function} (из списка)")
                return indicators
            return []
        
        indicators = []
        
        for date_str, values in time_series.items():
            try:
                # Парсим дату
                date_obj = None
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                except:
                    try:
                        date_obj = datetime.strptime(date_str, '%Y-%m')
                    except:
                        pass
                
                if not date_obj:
                    continue
                
                # Извлекаем значение (обычно это 'value' или первое числовое значение)
                value = None
                if isinstance(values, dict):
                    value_key = None
                    for k in ['value', 'Value', 'VALUE', '4. close', 'close']:
                        if k in values:
                            value_key = k
                            break
                    if value_key:
                        try:
                            value = float(values[value_key])
                        except:
                            pass
                elif isinstance(values, (int, float)):
                    value = float(values)
                
                if value is not None:
                    indicators.append({
                        'date': date_obj,
                        'value': value,
                        'indicator': function
                    })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка парсинга данных для {function} на дату {date_str}: {e}")
                continue
        
        logger.info(f"✅ Получено {len(indicators)} записей для {function}")
        return indicators
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Alpha Vantage для {function}: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при получении {function}: {e}")
        return []


def save_economic_indicators_to_db(indicators: List[Dict]):
    """
    Сохраняет экономические индикаторы в БД
    
    Args:
        indicators: Список индикаторов (каждый с полями: date, value, indicator)
    """
    if not indicators:
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    saved_count = 0
    
    with engine.begin() as conn:
        for ind in indicators:
            try:
                if not ind.get('date') or ind.get('value') is None:
                    continue
                
                indicator_name = ind.get('indicator', 'UNKNOWN')
                
                # Формируем контент
                content = f"{indicator_name}: {ind['value']}"
                
                # Проверяем дубликаты
                existing = conn.execute(
                    text("""
                        SELECT id FROM knowledge_base 
                        WHERE ticker = 'US_MACRO'
                        AND event_type = 'ECONOMIC_INDICATOR'
                        AND source LIKE :source_pattern
                        AND DATE(ts) = DATE(:ind_date)
                    """),
                    {
                        "source_pattern": f"%{indicator_name}%",
                        "ind_date": ind['date']
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
                        "ts": ind['date'],
                        "ticker": "US_MACRO",
                        "source": f"Alpha Vantage {indicator_name}",
                        "content": content,
                        "event_type": "ECONOMIC_INDICATOR",
                        "importance": "HIGH" if indicator_name in ['CPI', 'FEDERAL_FUNDS_RATE', 'GDP'] else "MEDIUM"
                    }
                )
                saved_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении индикатора {ind.get('indicator')}: {e}")
    
    logger.info(f"✅ Сохранено {saved_count} экономических индикаторов в БД")
    engine.dispose()


def fetch_technical_indicator(api_key: str, symbol: str, function: str, interval: str = 'daily', 
                              time_period: int = None, series_type: str = 'close', **kwargs) -> Dict:
    """
    Получает технический индикатор через Alpha Vantage
    
    Args:
        api_key: API ключ Alpha Vantage
        symbol: Тикер (например, 'IBM')
        function: Название функции (RSI, MACD, BBANDS, ADX, STOCH)
        interval: Интервал (daily, weekly, monthly)
        time_period: Период для индикатора (например, 14 для RSI)
        series_type: Тип серии (close, open, high, low)
        **kwargs: Дополнительные параметры для конкретных индикаторов
        
    Returns:
        Словарь с данными индикатора: {'date': datetime, 'value': float, ...}
        Или пустой словарь при ошибке
    """
    url = "https://www.alphavantage.co/query"
    params = {
        'function': function,
        'symbol': symbol,
        'interval': interval,
        'series_type': series_type,
        'apikey': api_key
    }
    
    if time_period:
        params['time_period'] = time_period
    
    # Специфичные параметры для разных индикаторов
    if function == 'RSI':
        if not time_period:
            params['time_period'] = 14
    elif function == 'MACD':
        params.setdefault('fastperiod', 12)
        params.setdefault('slowperiod', 26)
        params.setdefault('signalperiod', 9)
    elif function == 'BBANDS':
        if not time_period:
            params['time_period'] = 20
        params.setdefault('nbdevup', 2)
        params.setdefault('nbdevdn', 2)
    elif function == 'ADX':
        if not time_period:
            params['time_period'] = 14
    elif function == 'STOCH':
        params.setdefault('fastkperiod', 5)
        params.setdefault('slowkperiod', 3)
        params.setdefault('slowdperiod', 3)
    
    # Добавляем дополнительные параметры из kwargs
    params.update(kwargs)
    
    try:
        response = _get_with_retry(url, params)
        if not response:
            return {}
        response.raise_for_status()
        
        data = response.json()
        
        if 'Error Message' in data:
            logger.error(f"❌ Alpha Vantage ошибка для {function} ({symbol}): {data['Error Message']}")
            return {}
        
        if 'Note' in data:
            logger.warning(f"⚠️ Alpha Vantage лимит для {function} ({symbol}): {data['Note']}")
            return {}
        
        # Ответ только с ключом Information = лимит или премиум
        if list(data.keys()) == ['Information'] or (len(data) == 1 and 'Information' in data):
            msg = (data.get('Information') or '')[:200]
            logger.warning(
                f"⚠️ Alpha Vantage для {function} ({symbol}): ответ без данных. "
                f"Information: {msg}. Возможно лимит бесплатного плана или премиум-эндпоинт."
            )
            if "25 requests per day" in msg or "rate limit" in msg.lower():
                logger.warning(
                    "💡 На бесплатном плане в config.env установите ALPHAVANTAGE_FETCH_ECONOMIC=false и ALPHAVANTAGE_FETCH_TECHNICAL=false."
                )
            return {}
        
        # Извлекаем временной ряд (ключ может быть разным)
        time_series_key = None
        for key in data.keys():
            if 'Technical Analysis' in key or 'Time Series' in key:
                time_series_key = key
                break
        
        if not time_series_key:
            # Логируем структуру ответа для отладки
            available_keys = list(data.keys())[:10]
            logger.warning(
                f"⚠️ Не найдена временная серия для {function} ({symbol}). "
                f"Доступные ключи: {available_keys}. "
                f"Проверьте документацию Alpha Vantage для {function}."
            )
            # Для отладки: логируем первые несколько символов ответа
            if len(str(data)) < 500:
                logger.debug(f"Структура ответа для {function} ({symbol}): {data}")
            return {}
        
        time_series = data[time_series_key]
        
        # Берем последнее значение (самое свежее)
        if not time_series:
            return {}
        
        latest_date = max(time_series.keys())
        latest_data = time_series[latest_date]
        
        # Парсим дату
        date_obj = None
        try:
            date_obj = datetime.strptime(latest_date, '%Y-%m-%d')
        except:
            try:
                date_obj = datetime.strptime(latest_date, '%Y-%m-%d %H:%M:%S')
            except:
                pass
        
        result = {
            'date': date_obj or datetime.now(),
            'symbol': symbol,
            'indicator': function
        }
        
        # Извлекаем значения в зависимости от типа индикатора
        if function == 'RSI':
            result['rsi'] = float(latest_data.get('RSI', 0))
        elif function == 'MACD':
            result['macd'] = float(latest_data.get('MACD', 0))
            result['macd_signal'] = float(latest_data.get('MACD_Signal', 0))
            result['macd_hist'] = float(latest_data.get('MACD_Hist', 0))
        elif function == 'BBANDS':
            result['bbands_upper'] = float(latest_data.get('Real Upper Band', 0))
            result['bbands_middle'] = float(latest_data.get('Real Middle Band', 0))
            result['bbands_lower'] = float(latest_data.get('Real Lower Band', 0))
        elif function == 'ADX':
            result['adx'] = float(latest_data.get('ADX', 0))
        elif function == 'STOCH':
            result['stoch_k'] = float(latest_data.get('SlowK', 0))
            result['stoch_d'] = float(latest_data.get('SlowD', 0))
        
        logger.info(f"✅ Получен {function} для {symbol}: {latest_date}")
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Alpha Vantage для {function} ({symbol}): {e}")
        return {}
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка при получении {function} ({symbol}): {e}")
        return {}


def save_technical_indicators_to_db(indicators: List[Dict]):
    """
    Сохраняет технические индикаторы в таблицу quotes (обновляет существующие записи)
    
    Args:
        indicators: Список индикаторов (каждый с полями: date, symbol, и значениями индикаторов)
    """
    if not indicators:
        return
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    updated_count = 0
    
    with engine.begin() as conn:
        for ind in indicators:
            try:
                symbol = ind.get('symbol')
                ind_date = ind.get('date')
                
                if not symbol or not ind_date:
                    continue
                
                # Формируем UPDATE запрос динамически на основе доступных полей
                update_fields = []
                update_values = {}
                
                if 'rsi' in ind:
                    update_fields.append("rsi = :rsi")
                    update_values['rsi'] = ind['rsi']
                
                if 'macd' in ind:
                    update_fields.append("macd = :macd")
                    update_values['macd'] = ind['macd']
                    if 'macd_signal' in ind:
                        update_fields.append("macd_signal = :macd_signal")
                        update_values['macd_signal'] = ind['macd_signal']
                    if 'macd_hist' in ind:
                        update_fields.append("macd_hist = :macd_hist")
                        update_values['macd_hist'] = ind['macd_hist']
                
                if 'bbands_upper' in ind:
                    update_fields.append("bbands_upper = :bbands_upper")
                    update_values['bbands_upper'] = ind['bbands_upper']
                    if 'bbands_middle' in ind:
                        update_fields.append("bbands_middle = :bbands_middle")
                        update_values['bbands_middle'] = ind['bbands_middle']
                    if 'bbands_lower' in ind:
                        update_fields.append("bbands_lower = :bbands_lower")
                        update_values['bbands_lower'] = ind['bbands_lower']
                
                if 'adx' in ind:
                    update_fields.append("adx = :adx")
                    update_values['adx'] = ind['adx']
                
                if 'stoch_k' in ind:
                    update_fields.append("stoch_k = :stoch_k")
                    update_values['stoch_k'] = ind['stoch_k']
                    if 'stoch_d' in ind:
                        update_fields.append("stoch_d = :stoch_d")
                        update_values['stoch_d'] = ind['stoch_d']
                
                if not update_fields:
                    continue
                
                update_values['symbol'] = symbol
                update_values['ind_date'] = ind_date
                
                # Обновляем последнюю запись для этого тикера на эту дату или ближайшую
                query = f"""
                    UPDATE quotes 
                    SET {', '.join(update_fields)}
                    WHERE ticker = :symbol 
                    AND DATE(date) = DATE(:ind_date)
                """
                
                result = conn.execute(text(query), update_values)
                if result.rowcount == 0:
                    # Если записи нет на эту дату, пробуем обновить последнюю доступную
                    query_latest = f"""
                        UPDATE quotes 
                        SET {', '.join(update_fields)}
                        WHERE ticker = :symbol 
                        AND date = (
                            SELECT MAX(date) FROM quotes WHERE ticker = :symbol
                        )
                    """
                    conn.execute(text(query_latest), update_values)
                
                updated_count += 1
                
            except Exception as e:
                logger.error(f"❌ Ошибка при сохранении технического индикатора для {ind.get('symbol')}: {e}")
    
    logger.info(f"✅ Обновлено {updated_count} записей техническими индикаторами в БД")
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
    
    # Получаем earnings calendar (по умолчанию не сохраняем — записи «Earnings report for X» дают мало пользы, см. cleanup_calendar_noise.py)
    save_earnings = get_config_value("EARNINGS_CALENDAR_SAVE", "false").strip().lower() == "true"
    if save_earnings:
        logger.info("📅 Получение Earnings Calendar...")
        earnings = fetch_earnings_calendar(api_key)
        if earnings:
            save_earnings_to_db(earnings)
    else:
        logger.info("📅 Earnings Calendar пропущен (EARNINGS_CALENDAR_SAVE != true)")
    
    # Получаем новости (если указаны тикеры)
    if tickers:
        tickers_str = ','.join(tickers[:5])  # Alpha Vantage ограничивает количество тикеров
        logger.info(f"📰 Получение новостей для тикеров: {tickers_str}...")
        news = fetch_news_sentiment(api_key, tickers_str)
        if news:
            save_news_to_db(news)
    
    logger.info("✅ Завершено получение данных из Alpha Vantage")


def fetch_economic_indicators(api_key: str) -> List[Dict]:
    """
    Получает основные экономические индикаторы США.
    Между запросами пауза 1 сек (лимит бесплатного плана: 1 запрос/сек).
    """
    indicators = []
    delay = max(1.0, float(os.environ.get('ALPHAVANTAGE_MIN_DELAY_SEC', '1.0')))
    
    # CPI (Consumer Price Index) - monthly
    logger.info("📊 Получение CPI...")
    cpi_data = fetch_economic_indicator(api_key, 'CPI', interval='monthly')
    if cpi_data:
        indicators.extend(cpi_data)
        logger.info(f"   ✅ CPI: получено {len(cpi_data)} записей")
    else:
        logger.warning("   ⚠️ CPI: данные не получены")
    
    time.sleep(delay)
    # REAL_GDP - quarterly
    logger.info("📊 Получение GDP...")
    gdp_data = fetch_economic_indicator(api_key, 'REAL_GDP', interval='quarterly')
    if gdp_data:
        indicators.extend(gdp_data)
        logger.info(f"   ✅ GDP: получено {len(gdp_data)} записей")
    else:
        logger.warning("   ⚠️ GDP: данные не получены")
    
    time.sleep(delay)
    # Federal Funds Rate - monthly
    logger.info("📊 Получение Federal Funds Rate...")
    fed_rate_data = fetch_economic_indicator(api_key, 'FEDERAL_FUNDS_RATE', interval='monthly')
    if fed_rate_data:
        indicators.extend(fed_rate_data)
        logger.info(f"   ✅ Fed Rate: получено {len(fed_rate_data)} записей")
    else:
        logger.warning("   ⚠️ Fed Rate: данные не получены")
    
    time.sleep(delay)
    # Treasury Yield (10-year) - monthly
    logger.info("📊 Получение Treasury Yield...")
    treasury_data = fetch_economic_indicator(api_key, 'TREASURY_YIELD', interval='monthly')
    if treasury_data:
        indicators.extend(treasury_data)
        logger.info(f"   ✅ Treasury Yield: получено {len(treasury_data)} записей")
    else:
        logger.warning("   ⚠️ Treasury Yield: данные не получены")
    
    time.sleep(delay)
    # Unemployment - monthly
    logger.info("📊 Получение Unemployment...")
    unemployment_data = fetch_economic_indicator(api_key, 'UNEMPLOYMENT', interval='monthly')
    if unemployment_data:
        indicators.extend(unemployment_data)
        logger.info(f"   ✅ Unemployment: получено {len(unemployment_data)} записей")
    else:
        logger.warning("   ⚠️ Unemployment: данные не получены")
    
    logger.info(f"📊 Всего получено экономических индикаторов: {len(indicators)}")
    return indicators


def fetch_technical_indicators_for_tickers(api_key: str, tickers: List[str]) -> List[Dict]:
    """
    Получает технические индикаторы для списка тикеров
    
    Args:
        api_key: API ключ Alpha Vantage
        tickers: Список тикеров
        
    Returns:
        Список индикаторов
    """
    all_indicators = []
    # Пауза после таймаута/ошибки, чтобы не добивать API (секунды)
    delay_after_error = int(os.environ.get('ALPHAVANTAGE_DELAY_AFTER_ERROR', '15'))
    delay_between_tickers = int(os.environ.get('ALPHAVANTAGE_DELAY_BETWEEN_TICKERS', '15'))
    # Задержка между индикаторами внутри одного тикера (лимит: 5 запросов/минуту = 12 сек минимум)
    delay_between_indicators = int(os.environ.get('ALPHAVANTAGE_DELAY_BETWEEN_INDICATORS', '13'))
    
    for ticker in tickers:
        logger.info(f"📈 Получение технических индикаторов для {ticker}...")
        had_error = False
        
        indicators_list = [
            ('RSI', 'RSI', 14),
            ('MACD', 'MACD', None),
            ('BBANDS', 'BBANDS', 20),
            ('ADX', 'ADX', 14),
            ('STOCH', 'STOCH', None),
        ]
        
        for idx, (name, func_name, period) in enumerate(indicators_list):
            try:
                kwargs = {'time_period': period} if period else {}
                data = fetch_technical_indicator(api_key, ticker, func_name, **kwargs)
                if data:
                    all_indicators.append(data)
                else:
                    logger.debug(f"   {name} ({ticker}): данные не получены (пустой ответ или лимит)")
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                had_error = True
                logger.warning(f"⚠️ Таймаут/ошибка для {name} ({ticker}), пропуск. Пауза {delay_after_error} с.")
                time.sleep(delay_after_error)
            
            # Задержка между индикаторами (кроме последнего)
            if idx < len(indicators_list) - 1:
                time.sleep(delay_between_indicators)
        
        # Лимит бесплатного tier: 5 запросов/минуту — пауза между тикерами
        if ticker != tickers[-1]:  # Не ждём после последнего тикера
            time.sleep(delay_between_tickers)
    
    return all_indicators


def fetch_all_alphavantage_data(tickers: List[str] = None, include_economic: bool = True, 
                                 include_technical: bool = True):
    """
    Расширенная функция: получает все данные из Alpha Vantage
    
    Args:
        tickers: Список тикеров для отслеживания
        include_economic: Включать ли экономические индикаторы
        include_technical: Включать ли технические индикаторы
    """
    api_key = get_api_key()
    if not api_key:
        logger.warning("⚠️ ALPHAVANTAGE_KEY не настроен в config.env, пропускаем Alpha Vantage")
        return
    
    logger.info("🚀 Начало получения всех данных из Alpha Vantage")
    
    # Бесплатный план: 1 запрос/сек, 25 запросов/день. Часть эндпоинтов (напр. MACD) — премиум.
    min_delay = float(os.environ.get('ALPHAVANTAGE_MIN_DELAY_SEC', '1.0'))
    
    def _rate_limit():
        time.sleep(min_delay)
    
    # 1. Earnings Calendar (по умолчанию не сохраняем — шум в knowledge_base)
    save_earnings = get_config_value("EARNINGS_CALENDAR_SAVE", "false").strip().lower() == "true"
    if save_earnings:
        _rate_limit()
        logger.info("📅 Получение Earnings Calendar...")
        earnings = fetch_earnings_calendar(api_key)
        if earnings:
            save_earnings_to_db(earnings)
    else:
        logger.info("📅 Earnings Calendar пропущен (EARNINGS_CALENDAR_SAVE != true)")
    
    # 2. Новости (если указаны тикеры)
    if tickers:
        _rate_limit()
        tickers_str = ','.join(tickers[:5])
        logger.info(f"📰 Получение новостей для тикеров: {tickers_str}...")
        news = fetch_news_sentiment(api_key, tickers_str)
        if news:
            save_news_to_db(news)
    
    # 3. Экономические индикаторы (много запросов — на бесплатном плане лучше выключить)
    if include_economic:
        _rate_limit()
        logger.info("📊 Получение экономических индикаторов...")
        economic_indicators = fetch_economic_indicators(api_key)
        if economic_indicators:
            save_economic_indicators_to_db(economic_indicators)
    
    # 4. Технические индикаторы (часть — премиум; на бесплатном плане лучше выключить)
    if include_technical and tickers:
        _rate_limit()
        logger.info("📈 Получение технических индикаторов...")
        technical_indicators = fetch_technical_indicators_for_tickers(api_key, tickers[:3])  # Ограничиваем до 3 из-за лимитов
        if technical_indicators:
            save_technical_indicators_to_db(technical_indicators)
    
    logger.info("✅ Завершено получение всех данных из Alpha Vantage")


if __name__ == "__main__":
    import logging
    import os
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    # Полный поток: earnings + новости + экономические + технические индикаторы
    tickers = ['MSFT', 'SNDK', 'MU']
    fetch_all_alphavantage_data(
        tickers=tickers,
        include_economic=True,
        include_technical=True
    )

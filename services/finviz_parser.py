"""
Модуль для парсинга данных с Finviz.com
Получает технические индикаторы (RSI, перепроданные стоки) напрямую с проверенного ресурса
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
from typing import List, Dict, Optional
import logging
import time
import re
from urllib.parse import urlencode

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FinvizParser:
    """Парсер для получения данных с Finviz"""
    
    BASE_URL = "https://finviz.com"
    SCREENER_URL = f"{BASE_URL}/screener.ashx"
    
    def __init__(self, delay: float = 1.0):
        """
        Инициализация парсера
        
        Args:
            delay: Задержка между запросами (секунды) для избежания блокировки
        """
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
    
    def get_rsi_for_ticker(self, ticker: str) -> Optional[float]:
        """
        Получает значение RSI для конкретного тикера
        
        Args:
            ticker: Тикер акции (например, 'MSFT')
            
        Returns:
            Значение RSI (0-100) или None если не найдено
        """
        # Пропускаем валютные пары - Finviz их не поддерживает
        if '=X' in ticker.upper() or '/' in ticker:
            logger.info(f"   ⚠️ Пропуск валютной пары {ticker} - Finviz не поддерживает")
            return None
        
        try:
            # Переходим на страницу тикера
            url = f"{self.BASE_URL}/quote.ashx?t={ticker.upper()}"
            logger.info(f"📊 Получение RSI для {ticker} с {url}")
            
            response = self.session.get(url, timeout=10)
            
            # Проверяем на 404 - тикер не найден
            if response.status_code == 404:
                logger.warning(f"   ⚠️ Тикер {ticker} не найден на Finviz (404)")
                return None
            
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Ищем таблицу с техническими индикаторами
            # RSI обычно находится в таблице с классом 'snapshot-table2'
            tables = soup.find_all('table', class_='snapshot-table2')
            
            # Также пробуем найти по другим селекторам
            if not tables:
                tables = soup.find_all('table', {'class': lambda x: x and 'snapshot' in str(x).lower()})
            
            # Если не нашли по классу, ищем все таблицы
            if not tables:
                tables = soup.find_all('table')
            
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    # Широкая таблица: ищем ячейку с "RSI (14)" и берём значение из следующей
                    for i, cell in enumerate(cells):
                        label = cell.get_text(strip=True)
                        if 'RSI' in label.upper() and '(' in label:
                            try:
                                if i + 1 < len(cells):
                                    value = cells[i + 1].get_text(strip=True)
                                else:
                                    continue
                                clean_value = value.replace('%', '').replace(',', '').strip()
                                rsi_value = float(clean_value)
                                if 0 <= rsi_value <= 100:
                                    logger.info(f"   ✅ RSI для {ticker}: {rsi_value}")
                                    return rsi_value
                            except (ValueError, IndexError):
                                pass
                    # Классический вариант: две ячейки в строке (label, value)
                    if len(cells) >= 2:
                        label = cells[0].get_text(strip=True)
                        value = cells[1].get_text(strip=True)
                        if 'RSI' in label.upper():
                            try:
                                clean_value = value.replace('%', '').replace(',', '').strip()
                                rsi_value = float(clean_value)
                                if 0 <= rsi_value <= 100:
                                    logger.info(f"   ✅ RSI для {ticker}: {rsi_value}")
                                    return rsi_value
                            except ValueError:
                                pass
            
            # Альтернативный поиск: regex по тексту страницы (Finviz: "RSI (14) | 32.38")
            all_text = soup.get_text()
            rsi_pattern = r'RSI\s*\(\s*14\s*\)\s*(\d+\.?\d*)'
            matches = re.findall(rsi_pattern, all_text, re.IGNORECASE)
            if not matches:
                rsi_pattern = r'RSI[:\s]*(\d+\.?\d*)'
                matches = re.findall(rsi_pattern, all_text, re.IGNORECASE)
            if matches:
                try:
                    rsi_value = float(matches[0])
                    if 0 <= rsi_value <= 100:
                        logger.info(f"   ✅ RSI для {ticker} (найден через regex): {rsi_value}")
                        return rsi_value
                except (ValueError, IndexError):
                    pass
            
            logger.warning(f"   ⚠️ RSI не найден для {ticker}")
            return None
            
        except requests.exceptions.RequestException as e:
            logger.error(f"   ❌ Ошибка при запросе RSI для {ticker}: {e}")
            return None
        except Exception as e:
            logger.error(f"   ❌ Неожиданная ошибка при получении RSI для {ticker}: {e}")
            return None
        finally:
            time.sleep(self.delay)
    
    def get_oversold_stocks(self, exchange: str = 'NYSE', min_rsi: float = 30.0) -> List[Dict[str, any]]:
        """
        Получает список перепроданных стоков (RSI < min_rsi)
        
        Args:
            exchange: Биржа ('NYSE', 'NASDAQ', 'AMEX')
            min_rsi: Максимальное значение RSI для перепроданности (по умолчанию 30)
            
        Returns:
            Список словарей с информацией о стоках:
            [{'ticker': 'AAPL', 'rsi': 25.5, 'price': 150.0, ...}, ...]
        """
        try:
            # Параметры для screener
            # v=171 - это view с техническими индикаторами
            # f=exch_nyse - фильтр по бирже
            # ta_rsi_os - фильтр по перепроданности (oversold)
            params = {
                'v': '171',  # View с техническими индикаторами
                's': 'ta_mostactive',  # Сортировка по активности
                'f': f'exch_{exchange.lower()},ta_rsi_os{int(min_rsi)}',  # Фильтры
            }
            
            url = f"{self.SCREENER_URL}?{urlencode(params)}"
            logger.info(f"📊 Получение перепроданных стоков с {url}")
            
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Ищем таблицу с результатами screener
            table = soup.find('table', class_='screener_table')
            if not table:
                # Пробуем альтернативные селекторы
                table = soup.find('table', id='screener-table')
            
            if not table:
                logger.warning("   ⚠️ Таблица с результатами не найдена")
                return []
            
            stocks = []
            rows = table.find_all('tr')
            
            # Пропускаем заголовок
            for row in rows[1:]:
                cells = row.find_all('td')
                if len(cells) < 2:
                    continue
                
                try:
                    # Структура таблицы может варьироваться, ищем тикер и RSI
                    ticker = None
                    rsi = None
                    price = None
                    
                    for i, cell in enumerate(cells):
                        text = cell.get_text(strip=True)
                        # Тикер обычно в первой колонке или там где есть ссылка
                        if not ticker and cell.find('a'):
                            ticker = text
                        # RSI может быть в разных колонках, ищем число от 0 до 100
                        if not rsi:
                            try:
                                val = float(text)
                                if 0 <= val <= 100:
                                    rsi = val
                            except ValueError:
                                pass
                        # Цена обычно число с точкой
                        if not price:
                            try:
                                val = float(text.replace(',', ''))
                                if 1 <= val <= 10000:  # Разумный диапазон для цены
                                    price = val
                            except ValueError:
                                pass
                    
                    if ticker and rsi:
                        stocks.append({
                            'ticker': ticker,
                            'rsi': rsi,
                            'price': price,
                        })
                        logger.debug(f"   Найден: {ticker} - RSI: {rsi}")
                
                except Exception as e:
                    logger.debug(f"   Пропуск строки из-за ошибки: {e}")
                    continue
            
            logger.info(f"   ✅ Найдено {len(stocks)} перепроданных стоков")
            return stocks
            
        except requests.exceptions.RequestException as e:
            logger.error(f"   ❌ Ошибка при запросе перепроданных стоков: {e}")
            return []
        except Exception as e:
            logger.error(f"   ❌ Неожиданная ошибка при получении перепроданных стоков: {e}")
            return []
        finally:
            time.sleep(self.delay)
    
    def get_technical_indicators(self, ticker: str) -> Dict[str, Optional[float]]:
        """
        Получает все доступные технические индикаторы для тикера
        
        Args:
            ticker: Тикер акции
            
        Returns:
            Словарь с индикаторами:
            {
                'rsi': 45.5,
                'macd': 0.25,
                'sma_20': 150.0,
                ...
            }
        """
        try:
            url = f"{self.BASE_URL}/quote.ashx?t={ticker.upper()}"
            logger.info(f"📊 Получение технических индикаторов для {ticker}")
            
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            indicators = {}
            tables = soup.find_all('table', class_='snapshot-table2')
            
            # Словарь для маппинга названий индикаторов
            indicator_map = {
                'RSI (14)': 'rsi',
                'MACD': 'macd',
                'SMA20': 'sma_20',
                'SMA50': 'sma_50',
                'SMA200': 'sma_200',
                'Price': 'price',
                'Volume': 'volume',
            }
            
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) >= 2:
                        label = cells[0].get_text(strip=True)
                        value = cells[1].get_text(strip=True)
                        
                        # Проверяем маппинг
                        for key, mapped_key in indicator_map.items():
                            if key in label:
                                try:
                                    # Убираем знаки процента и другие символы
                                    clean_value = value.replace('%', '').replace(',', '').strip()
                                    num_value = float(clean_value)
                                    indicators[mapped_key] = num_value
                                    logger.debug(f"   {mapped_key}: {num_value}")
                                except ValueError:
                                    pass
                        
                        # Специальная обработка для RSI
                        if 'RSI' in label.upper() and 'rsi' not in indicators:
                            try:
                                rsi_value = float(value)
                                indicators['rsi'] = rsi_value
                            except ValueError:
                                pass
            
            logger.info(f"   ✅ Получено {len(indicators)} индикаторов для {ticker}")
            return indicators
            
        except requests.exceptions.RequestException as e:
            logger.error(f"   ❌ Ошибка при запросе индикаторов для {ticker}: {e}")
            return {}
        except Exception as e:
            logger.error(f"   ❌ Неожиданная ошибка при получении индикаторов для {ticker}: {e}")
            return {}
        finally:
            time.sleep(self.delay)


def get_rsi_for_tickers(tickers: List[str], delay: float = 1.0) -> Dict[str, Optional[float]]:
    """
    Удобная функция для получения RSI для списка тикеров
    
    Args:
        tickers: Список тикеров
        delay: Задержка между запросами
        
    Returns:
        Словарь {ticker: rsi_value}
    """
    parser = FinvizParser(delay=delay)
    results = {}
    
    for ticker in tickers:
        rsi = parser.get_rsi_for_ticker(ticker)
        results[ticker] = rsi
    
    return results


def get_oversold_stocks_list(exchange: str = 'NYSE', min_rsi: float = 30.0) -> List[Dict[str, any]]:
    """
    Удобная функция для получения списка перепроданных стоков
    
    Args:
        exchange: Биржа
        min_rsi: Максимальное значение RSI
        
    Returns:
        Список словарей с информацией о стоках
    """
    parser = FinvizParser()
    return parser.get_oversold_stocks(exchange=exchange, min_rsi=min_rsi)


if __name__ == "__main__":
    # Тестирование
    parser = FinvizParser()
    
    # Тест получения RSI для одного тикера
    print("Тест получения RSI:")
    rsi = parser.get_rsi_for_ticker("MSFT")
    print(f"MSFT RSI: {rsi}")
    
    # Тест получения перепроданных стоков
    print("\nТест получения перепроданных стоков:")
    oversold = parser.get_oversold_stocks(exchange='NYSE', min_rsi=30.0)
    print(f"Найдено перепроданных стоков: {len(oversold)}")
    for stock in oversold[:5]:  # Показываем первые 5
        print(f"  {stock}")


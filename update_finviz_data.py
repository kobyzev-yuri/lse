"""
Скрипт для обновления технических индикаторов (RSI) с Finviz.com
Использует проверенный ресурс вместо самостоятельного расчета
"""

import logging
from sqlalchemy import create_engine, text
from datetime import datetime
from typing import List, Optional

from config_loader import get_database_url
from services.finviz_parser import FinvizParser, get_rsi_for_tickers

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_tracked_tickers(engine) -> List[str]:
    """Получает список тикеров, которые отслеживаются в системе."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT DISTINCT ticker 
            FROM quotes 
            ORDER BY ticker
        """))
        tickers = [row[0] for row in result.fetchall()]
    return tickers


def update_rsi_for_ticker(engine, ticker: str, rsi: Optional[float]) -> bool:
    """
    Обновляет RSI для последней записи тикера
    
    Args:
        engine: SQLAlchemy engine
        ticker: Тикер акции
        rsi: Значение RSI (0-100) или None
        
    Returns:
        True если обновление успешно
    """
    if rsi is None:
        logger.warning(f"   ⚠️ RSI для {ticker} не получен, пропускаем")
        return False
    
    try:
        with engine.begin() as conn:
            # Обновляем RSI для последней записи тикера
            result = conn.execute(text("""
                UPDATE quotes
                SET rsi = :rsi
                WHERE ticker = :ticker
                  AND date = (
                      SELECT MAX(date) 
                      FROM quotes 
                      WHERE ticker = :ticker
                  )
            """), {
                "ticker": ticker,
                "rsi": float(rsi)
            })
            
            if result.rowcount > 0:
                logger.info(f"   ✅ RSI для {ticker} обновлен: {rsi}")
                return True
            else:
                logger.warning(f"   ⚠️ Не найдена последняя запись для {ticker}")
                return False
                
    except Exception as e:
        logger.error(f"   ❌ Ошибка при обновлении RSI для {ticker}: {e}")
        return False


def update_rsi_for_all_tickers(tickers: Optional[List[str]] = None, delay: float = 1.5) -> int:
    """
    Обновляет RSI для всех отслеживаемых тикеров или указанного списка
    
    Args:
        tickers: Список тикеров для обновления (если None - обновляет все из БД)
        delay: Задержка между запросами к Finviz (секунды)
        
    Returns:
        Количество успешно обновленных тикеров
    """
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    if tickers is None:
        tickers = get_tracked_tickers(engine)
        logger.info(f"📋 Найдено {len(tickers)} тикеров для обновления RSI: {', '.join(tickers)}")
    
    if not tickers:
        logger.warning("⚠️ Нет тикеров для обновления")
        engine.dispose()
        return 0
    
    # Получаем RSI для всех тикеров
    logger.info(f"📊 Получение RSI с Finviz для {len(tickers)} тикеров...")
    rsi_data = get_rsi_for_tickers(tickers, delay=delay)
    
    # Обновляем в базе данных
    updated_count = 0
    for ticker, rsi in rsi_data.items():
        if update_rsi_for_ticker(engine, ticker, rsi):
            updated_count += 1
    
    logger.info(f"✅ Обновление RSI завершено. Обновлено {updated_count} из {len(tickers)} тикеров")
    engine.dispose()
    return updated_count


def get_oversold_stocks_and_update(exchange: str = 'NYSE', min_rsi: float = 30.0) -> List[dict]:
    """
    Получает список перепроданных стоков с Finviz и обновляет их RSI в базе данных
    
    Args:
        exchange: Биржа ('NYSE', 'NASDAQ', 'AMEX')
        min_rsi: Максимальное значение RSI для перепроданности
        
    Returns:
        Список словарей с информацией о перепроданных стоках
    """
    from services.finviz_parser import get_oversold_stocks_list
    
    logger.info(f"📊 Получение перепроданных стоков (RSI < {min_rsi}) с биржи {exchange}...")
    oversold_stocks = get_oversold_stocks_list(exchange=exchange, min_rsi=min_rsi)
    
    if not oversold_stocks:
        logger.warning("   ⚠️ Перепроданные стоки не найдены")
        return []
    
    # Обновляем RSI в базе данных для найденных стоков
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    updated_count = 0
    for stock in oversold_stocks:
        ticker = stock.get('ticker')
        rsi = stock.get('rsi')
        if ticker and rsi:
            if update_rsi_for_ticker(engine, ticker, rsi):
                updated_count += 1
    
    logger.info(f"   ✅ Обновлено RSI для {updated_count} перепроданных стоков")
    engine.dispose()
    
    return oversold_stocks


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--oversold':
            # Получаем перепроданные стоки
            exchange = sys.argv[2] if len(sys.argv) > 2 else 'NYSE'
            min_rsi = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
            oversold = get_oversold_stocks_and_update(exchange=exchange, min_rsi=min_rsi)
            print(f"\n📊 Найдено {len(oversold)} перепроданных стоков:")
            for stock in oversold[:10]:  # Показываем первые 10
                print(f"  {stock['ticker']}: RSI={stock.get('rsi', 'N/A')}, Price={stock.get('price', 'N/A')}")
        else:
            # Обновляем RSI для указанных тикеров
            tickers = sys.argv[1].split(',')
            logger.info(f"Обновление RSI для указанных тикеров: {tickers}")
            update_rsi_for_all_tickers(tickers=tickers)
    else:
        # Обновляем RSI для всех тикеров из БД
        update_rsi_for_all_tickers()


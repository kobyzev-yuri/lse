"""
Скрипт для обновления цен котировок в базе данных.
Можно запускать вручную или через cron/scheduler для автоматического обновления.
"""

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re

from config_loader import get_database_url

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_tracked_tickers(engine):
    """Получает список тикеров, которые отслеживаются в системе."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT DISTINCT ticker 
            FROM quotes 
            ORDER BY ticker
        """))
        tickers = [row[0] for row in result.fetchall()]
    return tickers


def get_last_update_date(engine, ticker):
    """Получает дату последнего обновления для тикера."""
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT MAX(date) as last_date
            FROM quotes
            WHERE ticker = :ticker
        """), {"ticker": ticker})
        row = result.fetchone()
        if row and row[0]:
            return row[0]
    return None


def update_ticker_prices(engine, ticker, days_back=30):
    """
    Обновляет цены для конкретного тикера.
    Загружает данные за последние N дней или с последней даты обновления.
    """
    logger.info(f"📊 Обновление цен для {ticker}...")
    
    last_date = get_last_update_date(engine, ticker)
    
    if last_date:
        # Загружаем данные с последней даты + 1 день
        start_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
        logger.info(f"   Последнее обновление: {last_date}, загружаем с {start_date}")
        
        # yfinance требует период или интервал, используем период
        # Вычисляем количество дней между start_date и сегодня
        days_diff = (datetime.now().date() - last_date.date()).days
        if days_diff <= 0:
            logger.info(f"   ✅ Данные для {ticker} уже актуальны")
            return 0
        
        period = f"{min(days_diff + 5, 60)}d"  # Загружаем немного больше для надежности
    else:
        # Если данных нет, загружаем за последние N дней
        logger.info(f"   Данных нет, загружаем за последние {days_back} дней")
        period = f"{days_back}d"
    
    try:
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        
        if df.empty:
            logger.warning(f"   ⚠️ Нет данных для {ticker}")
            return 0
        
        # Если MultiIndex колонки, упрощаем структуру
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        
        # Рассчитываем базовые метрики
        df['sma_5'] = df['Close'].rolling(window=5).mean()
        df['volatility_5'] = df['Close'].rolling(window=5).std()
        
        # Удаляем строки с NaN значениями
        df = df.dropna(subset=['sma_5', 'volatility_5'])
        
        if df.empty:
            logger.warning(f"   ⚠️ Недостаточно данных для расчета метрик для {ticker}")
            return 0
        
        # Подготовка к вставке
        df = df.reset_index()
        
        # Фильтруем только новые данные (если есть last_date)
        if last_date:
            df = df[df['Date'] > last_date]
        
        if df.empty:
            logger.info(f"   ✅ Новых данных для {ticker} нет")
            return 0
        
        # Вставляем данные батчами
        inserted_count = 0
        with engine.begin() as conn:
            for _, row in df.iterrows():
                try:
                    conn.execute(text("""
                        INSERT INTO quotes (date, ticker, close, volume, sma_5, volatility_5, rsi)
                        VALUES (:date, :ticker, :close, :volume, :sma_5, :volatility_5, :rsi)
                        ON CONFLICT (date, ticker) DO NOTHING
                    """), {
                        "date": row['Date'], 
                        "ticker": ticker, 
                        "close": float(row['Close']),
                        "volume": int(row['Volume']) if pd.notna(row['Volume']) else None,
                        "sma_5": float(row['sma_5']) if pd.notna(row['sma_5']) else None,
                        "volatility_5": float(row['volatility_5']) if pd.notna(row['volatility_5']) else None,
                        "rsi": None  # RSI обновляется отдельно через update_finviz_data.py
                    })
                    inserted_count += 1
                except Exception as e:
                    logger.error(f"   ❌ Ошибка при вставке данных для {ticker} на {row['Date']}: {e}")
        
        logger.info(f"   ✅ Обновлено {inserted_count} записей для {ticker}")
        return inserted_count
        
    except Exception as e:
        logger.error(f"   ❌ Ошибка при обновлении {ticker}: {e}")
        return 0


def update_all_prices(tickers=None, days_back=30):
    """
    Обновляет цены для всех отслеживаемых тикеров или указанного списка.
    
    Args:
        tickers: Список тикеров для обновления (если None - обновляет все из БД)
        days_back: Количество дней назад для загрузки (если данных нет)
    """
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    if tickers is None:
        tickers = get_tracked_tickers(engine)
        logger.info(f"📋 Найдено {len(tickers)} тикеров для обновления: {', '.join(tickers)}")
    
    if not tickers:
        logger.warning("⚠️ Нет тикеров для обновления")
        return
    
    total_inserted = 0
    for ticker in tickers:
        try:
            count = update_ticker_prices(engine, ticker, days_back)
            total_inserted += count
        except Exception as e:
            logger.error(f"❌ Критическая ошибка при обновлении {ticker}: {e}")
    
    logger.info(f"✅ Обновление завершено. Всего добавлено {total_inserted} записей")
    engine.dispose()


if __name__ == "__main__":
    import sys
    
    # Можно передать тикеры через аргументы командной строки
    if len(sys.argv) > 1:
        tickers = sys.argv[1].split(',')
        logger.info(f"Обновление указанных тикеров: {tickers}")
        update_all_prices(tickers=tickers)
    else:
        # Обновляем все тикеры из БД
        update_all_prices()



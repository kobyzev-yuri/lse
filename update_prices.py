"""
Скрипт для обновления цен котировок в базе данных.
Можно запускать вручную или через cron/scheduler для автоматического обновления.
"""

import os

# Не использовать прокси при запросах к Yahoo (yfinance). Иначе при выключенном прокси — curl: Failed to connect to 127.0.0.1 port 1080
for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(key, None)

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


def update_ticker_prices(engine, ticker, days_back=30, force_days_back=None):
    """
    Обновляет цены для конкретного тикера (open, high, low, close и метрики).
    Загружает данные за последние N дней или с последней даты обновления.
    force_days_back: если задан, всегда загружать последние N дней (для backfill open/high/low).
    """
    logger.info(f"📊 Обновление цен для {ticker}...")
    
    last_date = None if force_days_back else get_last_update_date(engine, ticker)
    
    if force_days_back:
        period = f"{force_days_back}d"
        logger.info(f"   Загрузка последних {force_days_back} дней (backfill OHLC)")
    elif last_date:
        # Загружаем данные с последней даты + 1 день
        start_date = (last_date + timedelta(days=1)).strftime('%Y-%m-%d')
        logger.info(f"   Последнее обновление: {last_date}, загружаем с {start_date}")
        
        days_diff = (datetime.now().date() - last_date.date()).days
        if days_diff <= 0:
            logger.info(f"   ✅ Данные для {ticker} уже актуальны")
            return 0
        
        period = f"{min(days_diff + 5, 60)}d"
    else:
        logger.info(f"   Данных нет, загружаем за последние {days_back} дней")
        period = f"{days_back}d"
    
    try:
        # Ticker().history() стабильнее, чем yf.download(), при изменениях Yahoo API
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval="1d", auto_adjust=False)
        if df is None or df.empty:
            logger.warning(f"   ⚠️ Нет данных для {ticker}")
            return 0
        # Приводим имена колонок к ожидаемому виду (Open, High, Low, Close, Volume)
        df = df.rename_axis("Date").reset_index()
        for col in ("Open", "High", "Low", "Close", "Volume"):
            if col not in df.columns:
                logger.warning(f"   ⚠️ Нет колонки {col} для {ticker}")
                return 0

        # Рассчитываем базовые метрики
        df["sma_5"] = df["Close"].rolling(window=5).mean()
        df["volatility_5"] = df["Close"].rolling(window=5).std()
        
        # Удаляем строки с NaN значениями (первые 5 дней без sma_5)
        df = df.dropna(subset=["sma_5", "volatility_5"])
        if df.empty:
            logger.warning(f"   ⚠️ Недостаточно данных для расчета метрик для {ticker}")
            return 0
        
        # Фильтруем только новые данные (если есть last_date и не force_days_back)
        if last_date and not force_days_back:
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
                        INSERT INTO quotes (date, ticker, open, high, low, close, volume, sma_5, volatility_5, rsi)
                        VALUES (:date, :ticker, :open, :high, :low, :close, :volume, :sma_5, :volatility_5, :rsi)
                        ON CONFLICT (date, ticker) DO UPDATE SET
                            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                            close = EXCLUDED.close, volume = EXCLUDED.volume,
                            sma_5 = EXCLUDED.sma_5, volatility_5 = EXCLUDED.volatility_5
                    """), {
                        "date": row['Date'],
                        "ticker": ticker,
                        "open": float(row['Open']) if pd.notna(row.get('Open')) else None,
                        "high": float(row['High']) if pd.notna(row.get('High')) else None,
                        "low": float(row['Low']) if pd.notna(row.get('Low')) else None,
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


def update_all_prices(tickers=None, days_back=30, force_days_back=None):
    """
    Обновляет цены для всех отслеживаемых тикеров или указанного списка.

    Args:
        tickers: Список тикеров (если None — все из БД)
        days_back: Сколько дней загружать при первичной загрузке
        force_days_back: Если задан, перезагрузить последние N дней (заполнит open/high/low у старых строк)
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
            count = update_ticker_prices(engine, ticker, days_back, force_days_back=force_days_back)
            total_inserted += count
        except Exception as e:
            logger.error(f"❌ Критическая ошибка при обновлении {ticker}: {e}")

    logger.info(f"✅ Обновление завершено. Всего добавлено/обновлено {total_inserted} записей")
    engine.dispose()


if __name__ == "__main__":
    import sys

    # Примеры: python update_prices.py
    #          python update_prices.py SNDK,MSFT
    #          python update_prices.py --backfill 90   # заполнить open/high/low за последние 90 дней по всем тикерам
    tickers = None
    force_days_back = None
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--backfill":
            force_days_back = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 90
            i += 2
        elif a.startswith("--backfill="):
            force_days_back = int(a.split("=", 1)[1])
            i += 1
        elif not a.startswith("--"):
            tickers = [x.strip() for x in a.split(",")]
            i += 1
            break
        else:
            i += 1
    if tickers:
        logger.info(f"Обновление указанных тикеров: {tickers}")
    if force_days_back:
        logger.info(f"Режим backfill: перезагрузка последних {force_days_back} дней (open/high/low)")

    update_all_prices(tickers=tickers, force_days_back=force_days_back)



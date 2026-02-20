"""
Локальный расчёт RSI по истории close из БД.
Используется для валют (GBPUSD=X), товаров (GC=F) и акций, когда Finviz/Alpha Vantage недоступны.
"""

import logging
from typing import Optional

from sqlalchemy import create_engine, text

from config_loader import get_database_url

logger = logging.getLogger(__name__)

RSI_PERIOD = 14


def compute_rsi_from_closes(closes: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    """
    RSI(period) по ряду цен закрытия (последняя цена = текущая).
    
    Args:
        closes: список close от старых к новым (минимум period+1 элементов)
        period: период RSI (по умолчанию 14)
    
    Returns:
        RSI 0–100 или None при недостатке данных
    """
    if not closes or len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, min(len(closes), period + 1)):
        ch = closes[-(i + 1)] - closes[-i]  # изменение к более новой дате
        if ch > 0:
            gains.append(ch)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-ch)
    # Берём последние period изменений (от самой новой даты вглубь)
    if len(gains) < period:
        return None
    gains = gains[-period:]
    losses = losses[-period:]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def update_rsi_for_ticker(engine, ticker: str, period: int = RSI_PERIOD) -> bool:
    """
    Обновляет RSI для последней записи тикера по истории close из quotes.
    
    Returns:
        True если RSI посчитан и запись обновлена
    """
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT date, close
                FROM quotes
                WHERE ticker = :ticker
                ORDER BY date DESC
                LIMIT :limit
            """),
            {"ticker": ticker, "limit": period + 1},
        )
        rows = result.fetchall()
    
    if not rows or len(rows) < period + 1:
        logger.debug(f"   Недостаточно данных для RSI {ticker}: нужно {period + 1} дней")
        return False
    
    # rows от новых к старым; для RSI нужны close от старых к новым
    closes = [float(r[1]) for r in reversed(rows)]
    rsi = compute_rsi_from_closes(closes, period)
    if rsi is None:
        return False
    
    latest_date = rows[0][0]
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE quotes
                SET rsi = :rsi
                WHERE ticker = :ticker AND date = :date
            """),
            {"ticker": ticker, "rsi": rsi, "date": latest_date},
        )
    logger.info(f"   RSI {ticker}: {rsi:.1f} (локальный расчёт)")
    return True


def get_or_compute_rsi(engine, ticker: str, period: int = RSI_PERIOD) -> Optional[float]:
    """
    Возвращает RSI для тикера: из БД или вычисляет по close и обновляет запись.
    Вызывать при отсутствии RSI (например в боте/аналитике).
    """
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
            {"ticker": ticker},
        ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    if update_rsi_for_ticker(engine, ticker, period):
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                {"ticker": ticker},
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    return None


def update_rsi_for_all_tickers(
    engine=None,
    tickers: Optional[list[str]] = None,
    skip_tickers_with_rsi: bool = False,
) -> int:
    """
    Обновляет RSI по локальному расчёту для тикеров из БД.
    
    Args:
        engine: SQLAlchemy engine (если None — создаётся из config)
        tickers: список тикеров или None = все из quotes
        skip_tickers_with_rsi: если True, не трогать записи, у которых уже есть RSI
    
    Returns:
        количество тикеров, для которых RSI обновлён
    """
    if engine is None:
        engine = create_engine(get_database_url())
    
    if tickers is None:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker")
            )
            tickers = [r[0] for r in result]
    
    updated = 0
    for ticker in tickers:
        if skip_tickers_with_rsi:
            with engine.connect() as conn:
                r = conn.execute(
                    text("""
                        SELECT 1 FROM quotes
                        WHERE ticker = :ticker
                        ORDER BY date DESC LIMIT 1
                    """),
                    {"ticker": ticker},
                ).fetchone()
                if r:
                    r2 = conn.execute(
                        text("SELECT rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                        {"ticker": ticker},
                    ).fetchone()
                    if r2 and r2[0] is not None:
                        continue
        if update_rsi_for_ticker(engine, ticker):
            updated += 1
    return updated

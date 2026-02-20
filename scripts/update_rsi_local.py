#!/usr/bin/env python3
"""
Обновление RSI по локальному расчёту из истории close в quotes.
Используется для валют (GBPUSD=X), товаров (GC=F) и при отсутствии Finviz/Alpha Vantage.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from sqlalchemy import create_engine

from config_loader import get_database_url
from services.rsi_calculator import update_rsi_for_all_tickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    """Обновляет RSI для всех тикеров по локальному расчёту (close за 15 дней)."""
    engine = create_engine(get_database_url())
    # Сначала заполняем только те тикеры, у которых RSI ещё нет (например валюты/товары)
    updated = update_rsi_for_all_tickers(
        engine=engine,
        tickers=None,
        skip_tickers_with_rsi=True,
    )
    logger.info("✅ Локальный RSI: обновлено %s тикеров (без RSI)", updated)
    if updated == 0:
        # Можно запустить без skip и перезаписать все — раскомментируйте при необходимости
        # updated_all = update_rsi_for_all_tickers(engine=engine, skip_tickers_with_rsi=False)
        logger.info("   Все тикеры уже имеют RSI или недостаточно данных (нужно 15 дней close).")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Скрипт портфельной игры (торговый цикл по дневным стратегиям).

Тикеры по умолчанию из config.env: TRADING_CYCLE_TICKERS (если задан) или TICKERS_MEDIUM + TICKERS_LONG.
Аргумент: [тикеры] — через запятую, переопределяет config.

Cron: 0 9,13,17 * * 1-5  cd /path/to/lse && python scripts/trading_cycle_cron.py
  или с тикерами: ... trading_cycle_cron.py "MSFT,ORCL,AMD"
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from execution_agent import ExecutionAgent
from services.ticker_groups import get_tickers_for_portfolio_game
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(project_root / 'logs' / 'trading_cycle.log'),
        logging.StreamHandler()
    ]
)

if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1].strip():
            tickers = [t.strip() for t in sys.argv[1].strip().split(",") if t.strip()]
        else:
            tickers = get_tickers_for_portfolio_game()

        if not tickers:
            logging.warning("Тикеры не заданы (TRADING_CYCLE_TICKERS или TICKERS_MEDIUM/TICKERS_LONG в config.env, либо аргумент)")
            sys.exit(0)

        agent = ExecutionAgent()
        agent.run_for_tickers(tickers)
    except Exception as e:
        logging.error(f"Ошибка торгового цикла: {e}")
        sys.exit(1)




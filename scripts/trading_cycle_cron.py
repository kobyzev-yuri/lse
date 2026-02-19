#!/usr/bin/env python3
"""
Скрипт для автоматического торгового цикла через cron
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from execution_agent import ExecutionAgent
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(project_root / 'logs' / 'trading_cycle.log'),
        logging.StreamHandler()
    ]
)

# Список тикеров для торговли (можно настроить)
DEFAULT_TICKERS = ["MSFT", "SNDK", "GBPUSD=X", "GC=F"]  # GC=F = gold futures (XAUUSD=X не поддерживается Yahoo)

if __name__ == "__main__":
    try:
        # Можно передать тикеры через аргументы командной строки
        if len(sys.argv) > 1:
            tickers = [t.strip() for t in sys.argv[1].split(',')]
        else:
            tickers = DEFAULT_TICKERS
        
        agent = ExecutionAgent()
        agent.run_for_tickers(tickers)
    except Exception as e:
        logging.error(f"Ошибка торгового цикла: {e}")
        sys.exit(1)




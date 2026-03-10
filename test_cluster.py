import os
import sys

# Добавляем корневую директорию проекта в sys.path
project_root = '/home/cnn/lse'
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.cluster_manager import ClusterManager
import logging
logging.basicConfig(level=logging.WARNING)

manager = ClusterManager()
# tickers in joint games (from memory or config, but we can use get_all_ticker_groups)
from report_generator import get_engine, load_quotes
from services.ticker_groups import get_all_ticker_groups

all_tickers = get_all_ticker_groups()
print(f"All tickers: {all_tickers}")

res = manager.get_market_regimes(days=60, threshold=0.5)
import json
print(json.dumps(res, indent=2))

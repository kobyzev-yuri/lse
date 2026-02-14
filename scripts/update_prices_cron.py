#!/usr/bin/env python3
"""
Скрипт для автоматического обновления цен через cron
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from update_prices import update_all_prices
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(project_root / 'logs' / 'update_prices.log'),
        logging.StreamHandler()
    ]
)

if __name__ == "__main__":
    try:
        update_all_prices()
    except Exception as e:
        logging.error(f"Ошибка обновления цен: {e}")
        sys.exit(1)


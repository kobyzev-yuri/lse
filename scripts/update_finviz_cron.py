#!/usr/bin/env python3
"""
Скрипт для автоматического обновления RSI с Finviz через cron
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.update_finviz_data import update_all_rsi
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(project_root / 'logs' / 'update_finviz.log'),
        logging.StreamHandler()
    ]
)

if __name__ == "__main__":
    try:
        # Обновляем RSI для всех тикеров из БД
        # delay=1.5 секунды между запросами для избежания блокировки
        updated_count = update_all_rsi(delay=1.5)
        logging.info(f"✅ Обновление RSI завершено. Обновлено {updated_count} тикеров")
    except Exception as e:
        logging.error(f"Ошибка обновления RSI: {e}")
        sys.exit(1)

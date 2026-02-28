#!/usr/bin/env python3
"""Все сделки LITE в trade_history (любая стратегия)."""
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url
from sqlalchemy import create_engine, text

def main():
    url = get_database_url()
    e = create_engine(url)
    with e.connect() as c:
        r = c.execute(text("""
            SELECT id, ts, side, quantity, price, signal_type, strategy_name, ts_timezone
            FROM trade_history
            WHERE ticker = 'LITE'
            ORDER BY ts
        """))
        rows = r.fetchall()
    print("Все сделки LITE в trade_history:", len(rows))
    for row in rows:
        print(row)
    # также проверим, есть ли записи с ценами ~334, 315, 372, 323
    with e.connect() as c:
        r2 = c.execute(text("""
            SELECT id, ts, ticker, side, price, strategy_name
            FROM trade_history
            WHERE price BETWEEN 300 AND 400
            ORDER BY ts DESC
            LIMIT 20
        """))
        rows2 = r2.fetchall()
    print("\nЛюбые сделки с ценой 300–400 (последние 20):")
    for row in rows2:
        print(row)

if __name__ == "__main__":
    main()

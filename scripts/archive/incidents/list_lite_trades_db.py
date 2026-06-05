#!/usr/bin/env python3
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
            WHERE ticker = 'LITE' AND strategy_name = 'GAME_5M'
            ORDER BY ts
        """))
        rows = r.fetchall()
    print("LITE GAME_5M в БД:", len(rows), "сделок")
    for row in rows:
        print(row)

if __name__ == "__main__":
    main()

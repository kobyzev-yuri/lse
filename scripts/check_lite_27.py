#!/usr/bin/env python3
"""Проверка: есть ли в БД сделки LITE (GAME_5M) за 27 февраля."""
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url
from sqlalchemy import create_engine, text

def main():
    url = get_database_url()
    e = create_engine(url)
    # 27.02: если ts хранятся в MSK — окно 27 00:00–28 00:00; если в UTC — 27 18:00 UTC = 28 00:00 MSK
    # Берём широкое окно, чтобы поймать 27.02 в любой интерпретации
    with e.connect() as c:
        r = c.execute(text("""
            SELECT id, ts, side, quantity, price, signal_type, ts_timezone
            FROM trade_history
            WHERE ticker = 'LITE' AND strategy_name = 'GAME_5M'
              AND ts >= '2026-02-26 18:00:00' AND ts < '2026-02-28 06:00:00'
            ORDER BY ts
        """))
        rows = r.fetchall()
    print("LITE GAME_5M за 27.02 (широкое окно 26.02 18:00 – 28.02 06:00):", len(rows), "сделок")
    for row in rows:
        print(" ", row)
    if not rows:
        # Показать все LITE за последние дни, чтобы понять формат дат
        with e.connect() as c:
            r2 = c.execute(text("""
                SELECT id, ts, side, price, ts_timezone
                FROM trade_history
                WHERE ticker = 'LITE' AND strategy_name = 'GAME_5M'
                ORDER BY ts DESC
                LIMIT 10
            """))
            all_rows = r2.fetchall()
        print("\nВсе последние LITE GAME_5M (для сравнения дат):")
        for row in all_rows:
            print(" ", row)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Удаляет все сделки LITE из trade_history (любая стратегия).
После удаления отчёт /history (и «Последние сделки», и Positions) не покажет LITE.

Запуск:
  python scripts/clean_lite_from_db.py              # показать, что будет удалено (dry-run)
  python scripts/clean_lite_from_db.py --execute  # реально удалить
"""
import argparse
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

TICKER = "LITE"


def main():
    ap = argparse.ArgumentParser(description="Удалить все сделки LITE из trade_history")
    ap.add_argument("--execute", action="store_true", help="Выполнить удаление (без этого только показ)")
    args = ap.parse_args()

    from config_loader import get_database_url
    from sqlalchemy import create_engine, text

    engine = create_engine(get_database_url())

    with engine.connect() as conn:
        r = conn.execute(
            text("""
                SELECT id, ts, side, quantity, price, signal_type, strategy_name
                FROM trade_history
                WHERE ticker = :ticker
                ORDER BY ts, id
            """),
            {"ticker": TICKER},
        )
        rows = r.fetchall()

    if not rows:
        print(f"Сделок {TICKER} в БД нет. Ничего удалять не нужно.")
        return

    print(f"Найдено сделок {TICKER}: {len(rows)} (любая стратегия)")
    for row in rows:
        strat = row[6] if len(row) > 6 else ""
        print(f"  id={row[0]}  {row[2]}  {row[4]:.2f}  qty={row[3]}  strategy={strat!r}  {row[1]}")
    print()

    if not args.execute:
        print("Запустите с --execute, чтобы удалить эти строки.")
        return

    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM trade_history WHERE ticker = :ticker"),
            {"ticker": TICKER},
        )
        deleted = result.rowcount
    print(f"Удалено записей: {deleted}. LITE больше не фигурирует в /history.")


if __name__ == "__main__":
    main()

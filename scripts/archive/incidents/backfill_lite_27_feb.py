#!/usr/bin/env python3
"""
Добавляет в trade_history 4 сделки LITE за 27.02.2026 21:17 MSK (2 BUY, 2 SELL),
чтобы они отображались на графике /chart5m LITE за 27 число.

Данные из истории пользователя:
  LITE Long 334.69 → 315.20 (29 units)  27.02.2026 21:17 open/close
  LITE Long 372.09 → 323.38 (26 units)  27.02.2026 21:17 open/close

Запуск: python scripts/backfill_lite_27_feb.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 27.02.2026 21:17 MSK — храним как naive (приложение использует ts_timezone)
TS_MSK = "2026-02-27 21:17:00"
TZ = "Europe/Moscow"
STRATEGY = "GAME_5M"

TRADES = [
    {"side": "BUY", "price": 334.69, "qty": 29, "signal_type": "BUY"},
    {"side": "SELL", "price": 315.20, "qty": 29, "signal_type": "STOP_LOSS"},
    {"side": "BUY", "price": 372.09, "qty": 26, "signal_type": "BUY"},
    {"side": "SELL", "price": 323.38, "qty": 26, "signal_type": "STOP_LOSS"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Не писать в БД")
    args = ap.parse_args()
    dry = args.dry_run

    from config_loader import get_database_url
    from sqlalchemy import create_engine, text

    url = get_database_url()
    engine = create_engine(url)

    if dry:
        print("DRY RUN: в БД ничего не записывается")
    for t in TRADES:
        side = t["side"]
        price = float(t["price"])
        qty = int(t["qty"])
        signal = t["signal_type"]
        notional = price * qty
        commission = 0.0
        print(f"  {side} @ {price} qty={qty} {signal} ts={TS_MSK} {TZ}")
        if not dry:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO trade_history
                        (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone)
                        VALUES (:ts, 'LITE', :side, :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy, :ts_tz)
                    """),
                    {
                        "ts": TS_MSK,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "commission": commission,
                        "signal_type": signal,
                        "total_value": notional,
                        "strategy": STRATEGY,
                        "ts_tz": TZ,
                    },
                )
    if not dry:
        print("✅ Записано 4 сделки LITE за 27.02.2026 21:17 MSK.")
    else:
        print("Запустите без --dry-run для записи в БД.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Ручное закрытие открытой позиции GAME_5M (например, пропущенный тейк-профит).

Использование:
  python scripts/close_game_position.py SNDK [TAKE_PROFIT|STOP_LOSS|MANUAL]

По умолчанию тип выхода — TAKE_PROFIT (для «исправления» пропущенного тейка).
Текущая цена берётся из quotes (последняя close).
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main():
    if len(sys.argv) < 2:
        print("Использование: python scripts/close_game_position.py TICKER [TAKE_PROFIT|STOP_LOSS|MANUAL]")
        sys.exit(1)
    ticker = sys.argv[1].strip().upper()
    signal_type = (sys.argv[2].strip().upper() if len(sys.argv) > 2 else "TAKE_PROFIT") or "TAKE_PROFIT"
    if signal_type not in ("TAKE_PROFIT", "STOP_LOSS", "MANUAL", "SELL", "TIME_EXIT"):
        signal_type = "TAKE_PROFIT"

    from sqlalchemy import create_engine, text
    from config_loader import get_database_url
    from services.game_5m import get_open_position, close_position

    engine = create_engine(get_database_url())
    pos = get_open_position(ticker)
    if not pos:
        print(f"По {ticker} нет открытой позиции GAME_5M.")
        sys.exit(0)

    # Текущая цена из quotes
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT close FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
            {"ticker": ticker},
        ).fetchone()
    if not row or row[0] is None:
        print(f"Нет котировок для {ticker}. Укажите цену вручную или обновите quotes.")
        sys.exit(1)
    price = float(row[0])

    pnl_pct = close_position(ticker, price, signal_type)
    if pnl_pct is not None:
        print(f"Закрыто: {ticker} @ {price:.2f} ({signal_type}), PnL ≈ {pnl_pct:.2f}%")
    else:
        print(f"Не удалось закрыть позицию {ticker}.")


if __name__ == "__main__":
    main()

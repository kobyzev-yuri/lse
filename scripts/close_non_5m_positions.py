#!/usr/bin/env python3
"""
Закрытие открытых позиций «в долгую» (не GAME_5M): Mean Reversion, Momentum, Neutral и т.д.

Используйте, когда переходите только на игру 5m и хотите закрыть старые позиции по портфельным стратегиям.
Позиции GAME_5M не трогаются.

Использование:
  python scripts/close_non_5m_positions.py              # список + закрыть все не-GAME_5M
  python scripts/close_non_5m_positions.py --dry-run   # только показать, что будет закрыто
  python scripts/close_non_5m_positions.py TER ALAB    # закрыть только указанные тикеры (если они не GAME_5M)
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main():
    dry_run = "--dry-run" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--dry-run"]
    only_tickers = [t.strip().upper() for t in argv if t.strip()] if argv else None

    from report_generator import get_engine, load_trade_history, compute_open_positions, get_latest_prices
    from services.game_5m import get_open_position_any, close_position, GAME_5M_STRATEGY

    engine = get_engine()
    trades = load_trade_history(engine)
    pending = compute_open_positions(trades)

    # Только позиции не GAME_5M (игра в долгую)
    non_5m = [
        p for p in pending
        if (p.strategy_name or "").strip() and (p.strategy_name or "").strip() != GAME_5M_STRATEGY
    ]
    if only_tickers:
        non_5m = [p for p in non_5m if p.ticker.upper() in only_tickers]

    if not non_5m:
        print("Нет открытых позиций не-GAME_5M (все открытые — игра 5m или список пуст).")
        if only_tickers:
            print("  Указанные тикеры:", only_tickers)
        return

    print("Открытые позиции не-GAME_5M (будут закрыты по текущей цене из quotes):")
    for p in non_5m:
        print(f"  {p.ticker}: {p.quantity} @ {p.entry_price:.2f}  strategy={p.strategy_name or '—'}")
    print()

    prices = get_latest_prices(engine, [p.ticker for p in non_5m])
    for p in non_5m:
        if p.ticker not in prices:
            print(f"  ⚠️ Нет котировок для {p.ticker} — пропуск. Обновите quotes или закройте вручную через close_game_position.py с ценой.")
            continue
        price = float(prices[p.ticker])
        pos = get_open_position_any(p.ticker)
        if not pos:
            print(f"  ⚠️ Позиция {p.ticker} уже закрыта или не найдена.")
            continue
        if dry_run:
            pnl_pct = (price / p.entry_price - 1.0) * 100.0
            print(f"  [dry-run] Закрыли бы {p.ticker} @ {price:.2f}  PnL ≈ {pnl_pct:.2f}%")
            continue
        pnl_pct = close_position(p.ticker, price, "MANUAL", position=pos)
        if pnl_pct is not None:
            print(f"  ✅ {p.ticker} закрыта @ {price:.2f}, PnL ≈ {pnl_pct:.2f}%")
        else:
            print(f"  ❌ Не удалось закрыть {p.ticker}")

    if dry_run and non_5m:
        print("\nЗапустите без --dry-run, чтобы выполнить закрытие.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Мониторинг игры 5m: открытые позиции и сводка по закрытым сделкам (GAME_5M в trade_history).
Для просмотра из крона, по расписанию или вручную. Сделками управляет send_sndk_signal_cron.py.

Использование:
  python scripts/game5m_status.py              # по всем тикерам из TICKERS_FAST
  python scripts/game5m_status.py SNDK         # только SNDK
  python scripts/game5m_status.py SNDK 10     # SNDK, последние 10 закрытых сделок
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.ticker_groups import get_tickers_fast
from services.game_5m import get_open_position, get_recent_results, get_strategy_params


def main():
    tickers = [t.strip().upper() for t in sys.argv[1:2] if t.strip()] if len(sys.argv) >= 2 else None
    limit = 15
    if len(sys.argv) >= 3:
        try:
            limit = max(5, min(50, int(sys.argv[2])))
        except ValueError:
            pass
    if not tickers:
        tickers = get_tickers_fast()
    if not tickers:
        print("Нет тикеров. Задайте TICKERS_FAST в config.env или: game5m_status.py SNDK")
        return

    params = get_strategy_params()
    for ticker in tickers:
        pos = get_open_position(ticker)
        results = get_recent_results(ticker, limit=limit)
        print(f"\n--- Игра 5m: {ticker} ---")
        print(f"Параметры (config.env): стоп −{params['stop_loss_pct']}%, тейк +{params['take_profit_pct']}%, макс. {params['max_position_days']} дн.")
        if pos:
            ts = pos.get("entry_ts")
            ts_str = str(ts)[:19] if ts else "—"
            print(f"Открытая позиция: вход {ts_str} @ ${pos['entry_price']:.2f} · {pos['quantity']:.0f} шт. · {pos.get('entry_signal_type', '—')}")
        else:
            print("Открытой позиции нет.")
        if not results:
            print("Закрытых сделок пока нет.")
        else:
            pnls = [r["pnl_pct"] for r in results if r.get("pnl_pct") is not None]
            total = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            win_rate = (100.0 * wins / total) if total else 0
            avg_pnl = (sum(pnls) / total) if total else 0
            print(f"Закрытых сделок: {total}, Win rate: {wins}/{total} ({win_rate:.1f}%), Средний PnL: {avg_pnl:+.2f}%")
            for r in results[:5]:
                exit_ts = r.get("exit_ts") or "—"
                exit_str = str(exit_ts)[:16] if exit_ts != "—" else "—"
                pct = r.get("pnl_pct")
                pct_str = f"{pct:+.2f}%" if pct is not None else "—"
                print(f"  {exit_str} {r.get('exit_signal_type', '—')} PnL {pct_str}")
            if len(results) > 5:
                print(f"  ... и ещё {len(results) - 5}")
    print()


if __name__ == "__main__":
    main()

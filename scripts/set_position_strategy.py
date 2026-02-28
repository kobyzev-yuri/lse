#!/usr/bin/env python3
"""
Переназначение стратегии у открытой позиции (последний BUY по тикеру).

Используйте для позиций «вне игры»: тикер убран из GAME_5M, но позиция в БД
осталась с strategy_name=GAME_5M. После переназначения на Manual или Portfolio
в /pending будет отображаться новая стратегия, при закрытии в /closed — Entry с новой стратегией.

Использование:
  python scripts/set_position_strategy.py GC=F Manual
  python scripts/set_position_strategy.py GC=F Portfolio
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main():
    if len(sys.argv) < 3:
        print("Использование: python scripts/set_position_strategy.py TICKER STRATEGY")
        print("  STRATEGY: Manual, Portfolio, GAME_5M и т.д.")
        print("  Пример: python scripts/set_position_strategy.py GC=F Manual")
        sys.exit(1)
    ticker = sys.argv[1].strip().upper()
    strategy = (sys.argv[2] or "Manual").strip() or "Manual"

    from execution_agent import ExecutionAgent

    agent = ExecutionAgent()
    ok = agent.set_open_position_strategy(ticker, strategy)
    if ok:
        print(f"Стратегия последнего BUY по {ticker} изменена на «{strategy}». В /pending будет отображаться новая стратегия.")
    else:
        print(f"По {ticker} не найден BUY в trade_history (нет открытой позиции по этому тикеру).")
        sys.exit(1)


if __name__ == "__main__":
    main()

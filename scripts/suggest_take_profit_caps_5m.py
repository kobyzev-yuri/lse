#!/usr/bin/env python3
"""
Оценка разумных потолков тейка (GAME_5M_TAKE_PROFIT_PCT_<TICKER>) по 5m свечам за 7 торговых дней.

По каждой сессии (9:30–16:00 ET): макс. рост от открытия до хая сессии в %.
По тикерам: медиана, p70, p80 — предлагаемый потолок = округлённый p70 (достижим в ~70% сессий).

Запуск из корня репозитория:
  python scripts/suggest_take_profit_caps_5m.py
  python scripts/suggest_take_profit_caps_5m.py --tickers SNDK,MU,ASML
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main() -> None:
    from services.ticker_groups import get_tickers_game_5m
    from services.suggest_5m_params import compute_take_profit_suggestions

    tickers = get_tickers_game_5m()
    if not tickers:
        tickers = ["SNDK", "NBIS", "ASML", "MU", "LITE", "CIEN"]
    if "--tickers" in sys.argv:
        i = sys.argv.index("--tickers")
        if i + 1 < len(sys.argv):
            tickers = [t.strip().upper() for t in sys.argv[i + 1].split(",") if t.strip()]
    n_sessions = 7

    print("Тикеры:", ", ".join(tickers))
    print("Сессии: последние", n_sessions, "торговых дней (9:30–16:00 ET)")
    print()
    print(f"{'Ticker':<8} {'Сессий':<8} {'Медиана%':<10} {'p70%':<10} {'p80%':<10} {'Предлаг.':<10} {'Сейчас в конфиге':<20}")
    print("-" * 80)

    result = compute_take_profit_suggestions(tickers, n_sessions=n_sessions, fetch_days=10)
    for ticker in tickers:
        v = result.get(ticker, {})
        if "error" in v:
            print(f"{ticker:<8} {v['error']}")
            continue
        current = v.get("current_config", "—") or "—"
        print(f"{ticker:<8} {v.get('n_sessions', 0):<8} {v.get('median_pct', 0):<10.2f} {v.get('p70', 0):<10.2f} {v.get('p80', 0):<10.2f} {v.get('suggested_pct', 0):<10.1f} {str(current):<20}")

    print()
    print("Предлаг. = округлённый p70 (макс. рост от открытия до хая сессии за 7 дней).")
    print("Можно задать в config.env: GAME_5M_TAKE_PROFIT_PCT_<TICKER>=<число> (например GAME_5M_TAKE_PROFIT_PCT_ASML=4).")
    print("Автоматический ежедневный пересчёт отключён; значения переносите в config.env вручную при необходимости.")


if __name__ == "__main__":
    main()

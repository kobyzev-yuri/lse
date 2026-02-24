#!/usr/bin/env python3
"""
Проверка доступности 5m данных по быстрым тикерам (TICKERS_FAST).

Используется то же окно, что и для решения: все отметки от «сейчас» назад на 7 дней (Yahoo).
Есть хотя бы один бар — тикер считаем с 5m данными для игры и get_decision_5m.

Запуск: python scripts/check_fast_tickers_5m.py
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.ticker_groups import get_tickers_fast
from services.recommend_5m import has_5m_data, fetch_5m_ohlc, MAX_DAYS_5M


def main():
    tickers = get_tickers_fast()
    if not tickers:
        print("TICKERS_FAST пуст. Задайте в config.env: TICKERS_FAST=SNDK,NDK,LITE,NBIS")
        sys.exit(0)

    ok = []
    fail = []

    for t in tickers:
        try:
            if has_5m_data(t):
                df = fetch_5m_ohlc(t)
                bars = len(df) if df is not None else 0
                ok.append((t, bars))
            else:
                fail.append(t)
        except Exception as e:
            fail.append((t, str(e)))

    print("Быстрые тикеры (TICKERS_FAST) — 5m данные (Yahoo, окно до %d дн.):\n" % MAX_DAYS_5M)
    if ok:
        print("  С 5m данными (для решения и игры):")
        for t, bars in ok:
            print(f"    {t}: ок, баров: {bars}")
    if fail:
        print("  Нет 5m данных (cron будет пропускать):")
        for x in fail:
            if isinstance(x, tuple):
                print(f"    {x[0]}: {x[1]}")
            else:
                print(f"    {x}: нет баров")
        print("\n  Можно убрать тикеры из TICKERS_FAST или оставить — при появлении данных cron начнёт их обрабатывать.")

    if not ok:
        print("\n  Нет ни одного быстрого тикера с 5m. Проверьте Yahoo или задайте другие TICKERS_FAST.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

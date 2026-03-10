#!/usr/bin/env python3
"""
Проверка: какие тикеры из MEDIUM и LONG имеют нормальную корреляцию с FAST.
«Нормальная» = хотя бы одна пара (тикер, fast) с конечным коэффициентом за 30 дн.

Запуск из корня репозитория:
  python scripts/check_medium_long_correlation_with_fast.py
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def is_finite_corr(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def main() -> None:
    from services.ticker_groups import get_tickers_fast, get_tickers_medium, get_tickers_long
    from services.cluster_recommend import get_correlation_matrix

    fast = get_tickers_fast()
    medium = get_tickers_medium()
    long_ = get_tickers_long()
    fast_set = set(fast)

    if not fast:
        print("TICKERS_FAST пуст. Задайте в config.env.")
        sys.exit(1)

    # Кандидаты: MEDIUM + LONG без дубликатов, исключаем уже в FAST
    candidates = []
    seen = set()
    for t in medium + long_:
        if t and t not in seen and t not in fast_set:
            seen.add(t)
            candidates.append((t, "MEDIUM" if t in medium else "LONG"))

    if not candidates:
        print("Нет тикеров в MEDIUM/LONG вне FAST.")
        sys.exit(0)

    print("FAST:", ", ".join(fast))
    print("Проверка тикеров MEDIUM/LONG на корреляцию с FAST (30 дн., min_periods=5)...")
    print()

    keep_medium = []
    keep_long = []
    remove_medium = []
    remove_long = []

    for ticker, group in candidates:
        combo = list(fast) + [ticker]
        corr = get_correlation_matrix(combo, days=30, min_tickers_per_row=2)
        valid_with_fast = 0
        pairs = []
        if corr:
            for f in fast:
                v = corr.get(ticker, {}).get(f) or corr.get(f, {}).get(ticker)
                if is_finite_corr(v):
                    valid_with_fast += 1
                    pairs.append(f"{f}={float(v):+.2f}")
        if valid_with_fast >= 1:
            if group == "MEDIUM":
                keep_medium.append(ticker)
            else:
                keep_long.append(ticker)
            print(f"  {ticker} ({group}): OK — {valid_with_fast}/{len(fast)} пар с FAST: {', '.join(pairs[:5])}{'…' if len(pairs) > 5 else ''}")
        else:
            if group == "MEDIUM":
                remove_medium.append(ticker)
            else:
                remove_long.append(ticker)
            print(f"  {ticker} ({group}): убрать — нет корреляции с FAST (матрица: {'есть' if corr else 'нет'})")

    print()
    print("--- Итог ---")
    print("Оставляем в конфиге (нормальная корреляция с FAST):")
    print("  TICKERS_MEDIUM:", ",".join(keep_medium) if keep_medium else "(пусто)")
    print("  TICKERS_LONG:", ",".join(keep_long) if keep_long else "(пусто)")
    print("Убираем из конфига (на потом):")
    print("  MEDIUM:", ", ".join(remove_medium) if remove_medium else "—")
    print("  LONG:", ", ".join(remove_long) if remove_long else "—")


if __name__ == "__main__":
    main()

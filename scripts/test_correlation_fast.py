#!/usr/bin/env python3
"""
Тест гипотезы: корреляция по FAST тикерам (в т.ч. MSFT).
Проверяем, по каким парам получается коэффициент, по каким NaN — ищем «затык»-тикер.

Запуск из корня репозитория:
  python scripts/test_correlation_fast.py
  python scripts/test_correlation_fast.py --tickers SNDK,MU,MSFT
"""
from __future__ import annotations

import argparse
import os
import sys

# корень проекта в path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка корреляции по FAST тикерам")
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        help="Тикеры через запятую (по умолчанию — get_tickers_fast())",
    )
    parser.add_argument("--days", type=int, default=30, help="Окно для корреляции в днях")
    parser.add_argument(
        "--drop-one",
        action="store_true",
        help="Для каждого тикера: пересчитать матрицу без него и показать число заполненных пар",
    )
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        from services.ticker_groups import get_tickers_fast
        tickers = get_tickers_fast()
    if len(tickers) < 2:
        print("Нужно минимум 2 тикера. Задайте --tickers SNDK,MU,MSFT или настройте TICKERS_FAST в config.env")
        sys.exit(1)

    from services.cluster_recommend import get_correlation_matrix

    print("Тикеры:", ", ".join(tickers))
    print("Окно:", args.days, "дн.")
    print()

    # Полная матрица
    corr = get_correlation_matrix(tickers, days=args.days, min_tickers_per_row=2)
    if corr is None:
        print("Матрица не построилась (нет данных или ошибка).")
        if args.drop_one:
            print("\nПробуем по одному убирать тикеры:")
            for drop in tickers:
                rest = [t for t in tickers if t != drop]
                c = get_correlation_matrix(rest, days=args.days, min_tickers_per_row=2)
                n = _count_valid_pairs(c) if c else 0
                total_pairs = len(rest) * (len(rest) - 1) // 2
                print(f"  без {drop}: {n}/{total_pairs} пар заполнено" + (" ✓ матрица есть" if c else " (матрица пустая)"))
        sys.exit(2)

    # Статистика по парам
    n_valid, n_nan, pairs_nan = _count_valid_pairs(corr, return_nan_pairs=True)
    total = len(tickers) * (len(tickers) - 1) // 2
    print(f"Пар всего: {total}, с коэффициентом: {n_valid}, NaN: {n_nan}")
    if pairs_nan:
        print("Пары с NaN:", ", ".join(f"{a}-{b}" for a, b in sorted(pairs_nan)[:20]))
        if len(pairs_nan) > 20:
            print("  ... и ещё", len(pairs_nan) - 20)
    print()

    # По каждому тикеру: сколько пар с ним заполнено
    print("По тикерам (число пар с коэффициентом / всего пар с этим тикером):")
    for t in tickers:
        valid = 0
        total_t = 0
        for other in tickers:
            if other == t:
                continue
            total_t += 1
            v = corr.get(t, {}).get(other) or corr.get(other, {}).get(t)
            if v is not None and _isfinite(v):
                valid += 1
        print(f"  {t}: {valid}/{total_t}")
    print()

    # Таблица корреляций (верхний треугольник)
    print("Матрица (нижний треугольник, по строкам):")
    for i, a in enumerate(tickers):
        row = []
        for j, b in enumerate(tickers):
            if j >= i:
                row.append(" — ")
                continue
            v = corr.get(a, {}).get(b) or corr.get(b, {}).get(a)
            if v is not None and _isfinite(v):
                row.append(f"{v:+.2f}")
            else:
                row.append("NaN")
        print(f"  {a}: " + " ".join(f"{b}={row[j]}" for j, b in enumerate(tickers) if j < i))

    if args.drop_one:
        print("\n--- Убираем по одному тикеру ---")
        for drop in tickers:
            rest = [t for t in tickers if t != drop]
            c = get_correlation_matrix(rest, days=args.days, min_tickers_per_row=2)
            n = _count_valid_pairs(c) if c else 0
            total_pairs = len(rest) * (len(rest) - 1) // 2
            status = "✓" if total_pairs and n == total_pairs else f"{n}/{total_pairs}"
            print(f"  без {drop}: {status}")


def _isfinite(x: float) -> bool:
    import math
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _count_valid_pairs(
    corr: dict,
    return_nan_pairs: bool = False,
):
    """Считает пары с конечным коэффициентом. Опционально возвращает (valid, nan_count, list_nan_pairs)."""
    valid = 0
    nan_pairs: list[tuple[str, str]] = []
    seen = set()
    tickers = list(corr.keys())
    for a in tickers:
        for b in tickers:
            if a == b:
                continue
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            v = corr.get(a, {}).get(b) or corr.get(b, {}).get(a)
            if v is not None and _isfinite(v):
                valid += 1
            else:
                nan_pairs.append(key)
    if return_nan_pairs:
        return valid, len(nan_pairs), nan_pairs
    return valid


if __name__ == "__main__":
    main()

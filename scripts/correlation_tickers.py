#!/usr/bin/env python3
"""
Корреляции между тикерами по лог-доходностям (close из quotes).

Используются тикеры из TICKERS_FAST + TICKERS_MEDIUM + TICKERS_LONG (get_all_ticker_groups).
Данные: таблица quotes, колонка close. Лог-доходность: log(close_t / close_{t-1}).
Матрица корреляций: pandas .corr() по лог-доходностям.

Запуск:
  python scripts/correlation_tickers.py              # последние 60 дней, вывод в stdout
  python scripts/correlation_tickers.py --days 252  # год
  python scripts/correlation_tickers.py --csv out.csv # сохранить матрицу в CSV
"""

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pandas as pd

from report_generator import get_engine, load_quotes
from services.ticker_groups import get_all_ticker_groups


def main():
    parser = argparse.ArgumentParser(description="Корреляции между тикерами (лог-доходности)")
    parser.add_argument("--days", type=int, default=60, help="Число торговых дней (по умолч. 60)")
    parser.add_argument("--csv", type=str, default="", help="Путь к CSV для сохранения матрицы")
    parser.add_argument("--tickers", type=str, default="", help="Тикеры через запятую (иначе из config)")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = get_all_ticker_groups()
    if not tickers:
        print("Нет тикеров. Задайте TICKERS_FAST/MEDIUM/LONG в config.env или --tickers SNDK,MU,GC=F")
        sys.exit(1)

    engine = get_engine()
    quotes = load_quotes(engine, tickers)
    if quotes.empty:
        print("Нет данных в quotes по указанным тикерам.")
        sys.exit(1)

    prices = quotes.pivot_table(index="date", columns="ticker", values="close").sort_index()
    prices = prices.tail(max(args.days, 252)).replace(0, np.nan)
    log_returns = np.log(prices / prices.shift(1)).replace([np.inf, -np.inf], np.nan).dropna(how="all")
    if log_returns.empty:
        print("Нет лог-доходностей (проверьте quotes).")
        sys.exit(1)
    corr = log_returns.corr(min_periods=5)
    print(f"Корреляция лог-доходностей (до {len(log_returns)} дн., по парам ≥5 общих; NaN — мало данных):\n")
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda x: f"{x:.3f}" if pd.notna(x) else "—")
    print(corr.to_string())

    if args.csv:
        corr.to_csv(args.csv)
        print(f"\nМатрица сохранена: {args.csv}")

    sys.exit(0)


if __name__ == "__main__":
    main()

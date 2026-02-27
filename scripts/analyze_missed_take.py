#!/usr/bin/env python3
"""
Разбор «промахов» по тейк-профиту/стопу: когда цена достигла уровня тейка, но позиция не закрылась.

Использование:
  python scripts/analyze_missed_take.py SNDK [дата]
  Дата в формате YYYY-MM-DD (по умолчанию — вчера по серверу).

Скрипт:
  1. Берёт последний вход (BUY) по тикеру из trade_history (GAME_5M) на указанную дату или ранее.
  2. Скачивает 5m данные за этот день (yfinance).
  3. Эмулирует проверки «каждые 5 мин»: для каждого момента считает recent_bars_high_max (последние 6 баров),
     уровень тейка от entry и проверяет, сработал бы тейк (price_for_take >= entry * (1 + take_pct/100)).
  4. Выводит: первый бар, когда тейк был бы зафиксирован; фактические закрытия за день из trade_history.
  5. Подсказывает возможные причины промаха: последний запуск крона до 16:00 ET, после 16:00 крон не проверял (AFTER_HOURS).
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def main():
    if len(sys.argv) < 2:
        print("Использование: python scripts/analyze_missed_take.py TICKER [YYYY-MM-DD]")
        sys.exit(1)
    ticker = sys.argv[1].strip().upper()
    if len(sys.argv) >= 3:
        try:
            day = datetime.strptime(sys.argv[2].strip(), "%Y-%m-%d").date()
        except ValueError:
            print("Дата в формате YYYY-MM-DD")
            sys.exit(1)
    else:
        day = (datetime.now() - timedelta(days=1)).date()

    from sqlalchemy import create_engine, text
    from config_loader import get_database_url
    from services.game_5m import _effective_take_profit_pct, _effective_stop_loss_pct, get_open_position
    from services.recommend_5m import fetch_5m_ohlc

    engine = create_engine(get_database_url())

    # Последний BUY по тикеру на эту дату или ранее
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT ts, price, quantity
                FROM trade_history
                WHERE ticker = :ticker AND strategy_name = 'GAME_5M' AND side = 'BUY'
                  AND ts::date <= :day
                ORDER BY ts DESC
                LIMIT 1
            """),
            {"ticker": ticker, "day": day},
        ).fetchone()
    if not row:
        print(f"По {ticker} нет входа (BUY GAME_5M) на {day} или ранее.")
        sys.exit(0)

    entry_ts, entry_price, qty = row[0], float(row[1]), float(row[2])
    import pandas as pd
    entry_ts = pd.Timestamp(entry_ts)
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("America/New_York")
    take_pct = _effective_take_profit_pct(None)
    stop_pct = _effective_stop_loss_pct(None)
    take_level = entry_price * (1 + take_pct / 100.0)
    stop_level = entry_price * (1 - stop_pct / 100.0)

    print(f"Вход: {entry_ts} @ {entry_price:.2f} (тейк +{take_pct:.2f}% = {take_level:.2f}, стоп −{stop_pct:.2f}% = {stop_level:.2f})")
    print()

    # 5m данные: последние дни (Yahoo даёт до 7 дней), фильтр по нужной дате
    df = fetch_5m_ohlc(ticker, days=7)
    if df is None or df.empty or "High" not in df.columns:
        print(f"Нет 5m данных для {ticker}. Yahoo отдаёт 5m только за последние 1–7 дней.")
        sys.exit(0)

    # Приводим к дате для фильтра (без TZ)
    if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:
        df = df.copy()
        df["_date"] = df["datetime"].dt.tz_convert("America/New_York").dt.date
    else:
        df["_date"] = df["datetime"].dt.date
    df_day = df.loc[df["_date"] == day].drop(columns=["_date"]).sort_values("datetime").reset_index(drop=True)
    if df_day.empty:
        print(f"Нет 5m баров за {day}.")
        sys.exit(0)

    n_tail = 6  # как в recommend_5m — последние 6 баров для recent_bars_high_max
    first_take_bar = None
    first_stop_bar = None

    for i in range(len(df_day)):
        row = df_day.iloc[i]
        ts = row["datetime"]
        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])
        start_idx = max(0, i - n_tail + 1)
        window = df_day.iloc[start_idx : i + 1]
        bar_high_max = float(window["High"].max())
        bar_low_min = float(window["Low"].min())
        price_for_take = max(close, bar_high_max)
        price_for_stop = min(close, bar_low_min)
        bar_ts = pd.Timestamp(ts)
        if bar_ts.tzinfo is None and entry_ts.tzinfo is not None:
            bar_ts = bar_ts.tz_localize("America/New_York")
        if bar_ts < entry_ts:
            continue
        if first_take_bar is None and price_for_take >= take_level:
            first_take_bar = (ts, close, bar_high_max, price_for_take)
        if first_stop_bar is None and price_for_stop <= stop_level:
            first_stop_bar = (ts, close, bar_low_min, price_for_stop)

    # Фактические закрытия за день
    with engine.connect() as conn:
        closes = conn.execute(
            text("""
                SELECT ts, price, signal_type
                FROM trade_history
                WHERE ticker = :ticker AND strategy_name = 'GAME_5M' AND side = 'SELL'
                  AND ts::date = :day
                ORDER BY ts
            """),
            {"ticker": ticker, "day": day},
        ).fetchall()

    print("--- Эмуляция по 5m барам ---")
    if first_take_bar:
        ts, close, bar_high, price_for_take = first_take_bar
        print(f"Первый момент, когда тейк был бы зафиксирован: {ts} (close={close:.2f}, bar_high_max={bar_high:.2f}, price_for_take={price_for_take:.2f} >= {take_level:.2f})")
    else:
        print(f"За {day} цена ни разу не достигла уровня тейка {take_level:.2f} (по High последних 6 баров и Close).")
    if first_stop_bar:
        ts, close, bar_low, price_for_stop = first_stop_bar
        print(f"Первый момент стопа: {ts} (price_for_stop={price_for_stop:.2f} <= {stop_level:.2f})")

    print()
    print("--- Фактические закрытия за день (trade_history) ---")
    if closes:
        for r in closes:
            print(f"  {r[0]} @ {r[1]:.2f} ({r[2]})")
    else:
        print("  Нет закрытий за этот день.")

    print()
    print("--- Возможные причины промаха ---")
    print("• Крон 5m запускается каждые 5 мин только в REGULAR сессию (9:30–16:00 ET). После 16:00 раньше сразу выходили (AFTER_HOURS) и не проверяли тейк. Сейчас в AFTER_HOURS добавлена одна проверка открытых позиций по последним барам.")
    print("• Если тейк достигнут в 15:55–16:00, последний «полный» запуск мог быть в 15:55; в 16:00 крон уже видит AFTER_HOURS и теперь делает только проход по открытым позициям с последними данными.")
    print("• Запустите крон вручную после 16:00 ET (или следующий день): он закроет позицию по последнему доступному бару, если bar_high >= take_level.")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Корректировка цены выхода (SELL) в trade_history по котировке на момент сделки.

Для каждой SELL с strategy_name='GAME_5M' подставляется цена:
  - из 5m бара, содержащего момент ts (Close этого бара), если есть данные;
  - иначе из quotes (close за торговый день по ts в ET).

Использование:
  python scripts/correct_take_price_from_quotes.py [TICKER] [--dry-run]
  TICKER — опционально; без него обрабатываются все тикеры GAME_5M.
  --dry-run — только вывод, без UPDATE.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from config_loader import get_database_url
from services.game_5m import (
    GAME_5M_STRATEGY,
    TRADE_HISTORY_TZ,
    CHART_DISPLAY_TZ,
    trade_ts_to_et,
    COMMISSION_RATE,
)


def get_quote_at_moment(ticker: str, ts_et) -> float | None:
    """
    Цена по тикеру на момент ts_et (timezone-aware в America/New_York).
    Сначала 5m бар (Close), иначе quotes.close за этот день.
    """
    import pandas as pd

    try:
        pd_ts = pd.Timestamp(ts_et)
        if pd_ts.tzinfo is None:
            pd_ts = pd_ts.tz_localize(CHART_DISPLAY_TZ)
        else:
            pd_ts = pd_ts.tz_convert(CHART_DISPLAY_TZ)
    except Exception:
        return None

    date_str = pd_ts.strftime("%Y-%m-%d")
    end_d = pd_ts + pd.Timedelta(days=1)
    end_str = end_d.strftime("%Y-%m-%d")

    # 1) Пробуем 5m за этот день
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.history(start=date_str, end=end_str, interval="5m", auto_adjust=False)
        if df is not None and not df.empty:
            df = df.rename_axis("datetime").reset_index()
            for c in ("Open", "High", "Low", "Close"):
                if c not in df.columns:
                    break
            else:
                d = pd.to_datetime(df["datetime"])
                if d.dt.tz is None:
                    d = d.dt.tz_localize("America/New_York", ambiguous="infer")
                else:
                    d = d.dt.tz_convert("America/New_York")
                df = df.copy()
                df["_dt"] = d
                # Бар, в который попадает ts_et: bar_start <= ts_et < bar_start+5min
                for _, row in df.iterrows():
                    bar_start = row["_dt"]
                    bar_end = bar_start + pd.Timedelta(minutes=5)
                    if bar_start <= pd_ts < bar_end:
                        return float(row["Close"])
                # Иначе ближайший бар по времени (последний до или первый после)
                df = df.sort_values("_dt")
                idx = df["_dt"].searchsorted(pd_ts, side="right")
                if idx > 0:
                    return float(df.iloc[idx - 1]["Close"])
                if len(df) > 0:
                    return float(df.iloc[0]["Close"])
    except Exception as e:
        print(f"  5m для {ticker} @ {date_str}: {e}", file=sys.stderr)

    # 2) Fallback: quotes за этот день
    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT close FROM quotes
                    WHERE ticker = :ticker
                      AND date >= :d::timestamp AND date < :d::timestamp + interval '1 day'
                    ORDER BY date DESC LIMIT 1
                """),
                {"ticker": ticker, "d": date_str},
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        print(f"  quotes для {ticker} @ {date_str}: {e}", file=sys.stderr)

    return None


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        sys.argv.remove("--dry-run")
    ticker_filter = sys.argv[1].strip().upper() if len(sys.argv) > 1 else None

    engine = create_engine(get_database_url())

    # Все SELL по GAME_5M (с ts_timezone для перевода в ET)
    with engine.connect() as conn:
        q = text("""
            SELECT id, ts, ticker, side, quantity, price, signal_type, total_value,
                   ts_timezone
            FROM trade_history
            WHERE strategy_name = :strategy AND side = 'SELL'
            ORDER BY ts ASC
        """)
        params = {"strategy": GAME_5M_STRATEGY}
        if ticker_filter:
            q = text("""
                SELECT id, ts, ticker, side, quantity, price, signal_type, total_value,
                       ts_timezone
                FROM trade_history
                WHERE strategy_name = :strategy AND side = 'SELL' AND ticker = :ticker
                ORDER BY ts ASC
            """)
            params["ticker"] = ticker_filter
        rows = conn.execute(q, params).fetchall()

    if not rows:
        print("Нет SELL по GAME_5M для обработки.")
        return

    print(f"Найдено SELL: {len(rows)}. {'(dry-run, без UPDATE)' if dry_run else ''}")
    updated = 0
    for r in rows:
        sid, ts, ticker, side, qty, old_price, signal_type, total_value, ts_tz = (
            r[0], r[1], r[2], r[3], float(r[4]) if r[4] else 0,
            float(r[5]) if r[5] else 0, r[6], float(r[7]) if r[7] else 0, r[8],
        )
        ts_et = trade_ts_to_et(ts, source_tz=ts_tz or TRADE_HISTORY_TZ)
        new_price = get_quote_at_moment(ticker, ts_et)
        if new_price is None:
            print(f"  id={sid} {ticker} {ts} — нет котировки на момент сделки, пропуск")
            continue
        if abs(new_price - old_price) < 0.005:
            continue
        new_total = qty * new_price
        new_commission = new_total * COMMISSION_RATE
        print(f"  id={sid} {ticker} {ts} {signal_type}: {old_price:.2f} → {new_price:.2f} (total_value: {total_value:.2f} → {new_total:.2f})")
        if not dry_run:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        UPDATE trade_history
                        SET price = :price, total_value = :total_value, commission = :commission
                        WHERE id = :id
                    """),
                    {
                        "id": sid,
                        "price": new_price,
                        "total_value": new_total,
                        "commission": new_commission,
                    },
                )
        updated += 1

    print(f"Скорректировано записей: {updated}")


if __name__ == "__main__":
    main()

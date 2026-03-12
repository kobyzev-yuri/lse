#!/usr/bin/env python3
"""
Коррекция цены выхода TAKE_PROFIT в trade_history по bar_high.

Для записей GAME_5M с signal_type='TAKE_PROFIT' подставляется цена = max(High)
последних 6 баров на момент закрытия (как при принятии решения в кроне),
вместо ранее записанного exit_bar_close.

Использование:
  python scripts/correct_take_profit_high.py [TICKER] [--dry-run]
  TICKER — опционально; без него обрабатываются все тикеры GAME_5M.
  --dry-run — только вывод, без UPDATE.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd
from sqlalchemy import create_engine, text
from config_loader import get_database_url
from services.game_5m import (
    GAME_5M_STRATEGY,
    TRADE_HISTORY_TZ,
    COMMISSION_RATE,
    trade_ts_to_et,
)


N_BARS = 6  # как recent_bars_high_max в кроне


def get_bar_high_at_exit(ticker: str, ts_et) -> float | None:
    """
    Максимум High по последним N_BARS 5m-барам на момент ts_et (timezone-aware в America/New_York).
    Возвращает None, если нет данных.
    """
    try:
        pd_ts = pd.Timestamp(ts_et)
        if pd_ts.tzinfo is None:
            pd_ts = pd_ts.tz_localize("America/New_York")
        else:
            pd_ts = pd_ts.tz_convert("America/New_York")
    except Exception:
        return None

    date_str = pd_ts.strftime("%Y-%m-%d")
    end_d = pd_ts + pd.Timedelta(days=1)
    end_str = end_d.strftime("%Y-%m-%d")

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.history(start=date_str, end=end_str, interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.rename_axis("datetime").reset_index()
        for c in ("Open", "High", "Low", "Close"):
            if c not in df.columns:
                return None
        d = pd.to_datetime(df["datetime"])
        if d.dt.tz is None:
            d = d.dt.tz_localize("America/New_York", ambiguous="infer")
        else:
            d = d.dt.tz_convert("America/New_York")
        df = df.copy()
        df["_dt"] = d
        # Бары, закончившиеся к моменту ts (бар 10:30–10:35 учитываем если ts >= 10:35)
        # В Yahoo datetime бара — начало; конец бара = начало + 5 min
        df["_bar_end"] = df["_dt"] + pd.Timedelta(minutes=5)
        mask = df["_bar_end"] <= pd_ts
        past = df.loc[mask].sort_values("_dt", ascending=False)
        if past.empty:
            # Fallback: последний бар до ts
            past = df[df["_dt"] <= pd_ts].sort_values("_dt", ascending=False)
        tail = past.head(N_BARS)
        if tail.empty:
            return None
        return float(tail["High"].max())
    except Exception as e:
        print(f"  5m bar_high для {ticker} @ {date_str}: {e}", file=sys.stderr)
        return None


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        sys.argv.remove("--dry-run")
    ticker_filter = sys.argv[1].strip().upper() if len(sys.argv) > 1 else None

    engine = create_engine(get_database_url())

    with engine.connect() as conn:
        q = text("""
            SELECT id, ts, ticker, side, quantity, price, signal_type, total_value,
                   ts_timezone
            FROM trade_history
            WHERE strategy_name = :strategy AND side = 'SELL' AND signal_type = 'TAKE_PROFIT'
            ORDER BY ts ASC
        """)
        params = {"strategy": GAME_5M_STRATEGY}
        if ticker_filter:
            q = text("""
                SELECT id, ts, ticker, side, quantity, price, signal_type, total_value,
                       ts_timezone
                FROM trade_history
                WHERE strategy_name = :strategy AND side = 'SELL' AND signal_type = 'TAKE_PROFIT'
                  AND ticker = :ticker
                ORDER BY ts ASC
            """)
            params["ticker"] = ticker_filter
        rows = conn.execute(q, params).fetchall()

    if not rows:
        print("Нет TAKE_PROFIT по GAME_5M для обработки.")
        return

    print(f"Найдено TAKE_PROFIT: {len(rows)}. {'(dry-run, без UPDATE)' if dry_run else ''}")
    updated = 0
    for r in rows:
        sid, ts, ticker, side, qty, old_price, signal_type, total_value, ts_tz = (
            r[0], r[1], r[2], r[3], float(r[4]) if r[4] else 0,
            float(r[5]) if r[5] else 0, r[6], float(r[7]) if r[7] else 0, r[8],
        )
        ts_et = trade_ts_to_et(ts, source_tz=ts_tz or TRADE_HISTORY_TZ)
        bar_high = get_bar_high_at_exit(ticker, ts_et)
        if bar_high is None or bar_high <= 0:
            print(f"  id={sid} {ticker} {ts} — нет 5m bar_high, пропуск")
            continue
        if abs(bar_high - old_price) < 0.005:
            continue
        new_total = qty * bar_high
        new_commission = new_total * COMMISSION_RATE
        print(f"  id={sid} {ticker} {ts} TAKE_PROFIT: {old_price:.2f} → {bar_high:.2f} (total_value: {total_value:.2f} → {new_total:.2f})")
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
                        "price": bar_high,
                        "total_value": new_total,
                        "commission": new_commission,
                    },
                )
        updated += 1

    print(f"Скорректировано записей: {updated}")


if __name__ == "__main__":
    main()

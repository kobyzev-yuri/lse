#!/usr/bin/env python3
"""
Восстановление импульса при входе (entry_impulse_pct / momentum_2h_pct) для старых BUY в trade_history.

Для BUY без context_json или без momentum_2h_pct подгружаем 5m свечи (Yahoo, до ~7 дней),
находим бар на момент входа и считаем импульс 2ч как в recommend_5m: (close_now / close_24_bars_ago - 1) * 100.
Обновляем context_json у записи.

Ограничение: Yahoo отдаёт 5m только за последние ~7 дней, поэтому восстановить можно только входы за этот период.

Запуск из корня репозитория:
  python scripts/backfill_entry_impulse_5m.py
  python scripts/backfill_entry_impulse_5m.py --dry-run
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Как в recommend_5m
BARS_2H = 24


def _buy_ts_to_et(ts) -> "pd.Timestamp|None":
    from services.game_5m import trade_ts_to_et, TRADE_HISTORY_TZ
    return trade_ts_to_et(ts, source_tz=TRADE_HISTORY_TZ)


def _momentum_2h_at_ts(df_5m, buy_ts_et) -> float | None:
    """Импульс 2ч на момент buy_ts_et: по барам до этого момента, close[-1]/close[-(1+24)] - 1."""
    import pandas as pd
    if df_5m is None or df_5m.empty or "datetime" not in df_5m.columns or "Close" not in df_5m.columns:
        return None
    df = df_5m[df_5m["datetime"] <= buy_ts_et].copy()
    if df.empty or len(df) < BARS_2H + 1:
        return None
    df = df.sort_values("datetime").tail(BARS_2H + 1)
    closes = df["Close"]
    price = float(closes.iloc[-1])
    price_2h_ago = float(closes.iloc[0])
    if price_2h_ago <= 0:
        return None
    return ((price / price_2h_ago) - 1.0) * 100.0


def main() -> None:
    from sqlalchemy import create_engine, text
    from config_loader import get_database_url
    from services.recommend_5m import fetch_5m_ohlc, filter_to_last_n_us_sessions
    import pandas as pd

    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("Режим dry-run: обновления в БД не выполняются.")

    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, ts, ticker, context_json
                FROM public.trade_history
                WHERE strategy_name = 'GAME_5M' AND side = 'BUY'
                ORDER BY ts DESC
            """)
        ).fetchall()

    # Оставляем только те, у которых нет momentum_2h_pct в context_json
    to_backfill = []
    for r in rows:
        id_, ts, ticker, ctx = r[0], r[1], r[2], r[3]
        if ctx:
            try:
                obj = json.loads(ctx) if isinstance(ctx, str) else ctx
                if isinstance(obj, dict) and obj.get("momentum_2h_pct") is not None:
                    continue
            except Exception:
                pass
        to_backfill.append({"id": id_, "ts": ts, "ticker": ticker})

    if not to_backfill:
        print("Нет BUY без импульса в context_json.")
        return

    print(f"Найдено BUY без momentum_2h_pct: {len(to_backfill)}. Загружаем 5m по тикерам…")

    updated = 0
    skipped_no_data = 0
    skipped_old = 0
    ticker_cache = {}

    for row in to_backfill:
        buy_id, buy_ts, ticker = row["id"], row["ts"], row["ticker"]
        buy_et = _buy_ts_to_et(buy_ts)
        if buy_et is None:
            skipped_old += 1
            continue
        buy_et = pd.Timestamp(buy_et)
        if ticker not in ticker_cache:
            df = fetch_5m_ohlc(ticker, days=10)
            if df is not None and not df.empty:
                df = filter_to_last_n_us_sessions(df, n=7)
            ticker_cache[ticker] = df
        df = ticker_cache[ticker]
        if df is None or df.empty:
            skipped_no_data += 1
            continue
        mom = _momentum_2h_at_ts(df, buy_et)
        if mom is None:
            skipped_no_data += 1
            continue
        ctx = {"momentum_2h_pct": round(mom, 4), "entry_impulse_pct": round(mom, 4), "backfilled": True}
        ctx_str = json.dumps(ctx, ensure_ascii=False)
        if not dry_run:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE public.trade_history SET context_json = :ctx WHERE id = :id"),
                    {"ctx": ctx_str, "id": buy_id}
                )
        updated += 1
        print(f"  {ticker} id={buy_id} ts={buy_ts} → momentum_2h_pct={mom:+.2f}%")

    print()
    print(f"Обновлено: {updated}, пропущено (нет 5m/старый): {skipped_no_data + skipped_old}.")
    if dry_run and updated:
        print("Запустите без --dry-run для записи в БД.")


if __name__ == "__main__":
    main()

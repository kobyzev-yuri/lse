#!/usr/bin/env python3
"""
Сверка GAME_5M за окно --days:

1) Те же BUY из trade_history — реплей выхода по 5m и по 30m (импульс 2ч с соответствующей сетки).
2) Опционально --full-30m-sim: на том же недельном окне (ET) полная эмуляция стратегии на 30m —
   свои входы (техника + по умолчанию KB/sentiment и VIX как в get_decision_5m) и свои выходы
   (should_close_position), без привязки к датам входов из БД.

Пример:
  python scripts/backtest_game5m_take_5m_vs_30m.py --days 7 --exchange US
  python scripts/backtest_game5m_take_5m_vs_30m.py --days 7 --full-30m-sim --json-out local/game5m_take_5m_vs_30m.json
  # только автономная 30m-симуляция с KB (без реплея по BUY): тикеры из конфига или --tickers
  python scripts/backtest_game5m_take_5m_vs_30m.py --days 7 --full-30m-sim --sim-30m-only --exchange US --json-out logs/sim30m.json
  python scripts/backtest_game5m_take_5m_vs_30m.py --days 7 --full-30m-sim --sim-30m-only --tickers SNDK,MU --json-out logs/sim30m.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url

from services.ticker_groups import get_tickers_game_5m
from services.game_5m_take_replay import (
    load_bars_30m_for_replay,
    load_bars_5m_for_replay,
    log_return_pnl,
    momentum_from_30m_slice,
    momentum_from_5m_slice,
    replay_game5m_on_bars,
    simulate_game5m_30m_strategy_on_window,
    trade_ts_to_et,
)


def _fetch_buys(engine, days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = text(
        """
        SELECT id, ts, ticker, price, quantity, take_profit, stop_loss, context_json
        FROM trade_history
        WHERE strategy_name = 'GAME_5M' AND side = 'BUY' AND ts >= :cutoff
        ORDER BY ts ASC, id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, {"cutoff": cutoff}).mappings().all()
    return [dict(r) for r in rows]


def _fetch_next_sell(engine, ticker: str, buy_ts: Any, buy_id: int) -> Optional[dict[str, Any]]:
    q = text(
        """
        SELECT id, ts, price, signal_type
        FROM trade_history
        WHERE strategy_name = 'GAME_5M' AND side = 'SELL' AND ticker = :ticker
          AND (ts > :buy_ts OR (ts = :buy_ts AND id > :buy_id))
        ORDER BY ts ASC, id ASC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(q, {"ticker": ticker, "buy_ts": buy_ts, "buy_id": buy_id}).mappings().first()
    return dict(row) if row else None


def main() -> None:
    p = argparse.ArgumentParser(description="GAME_5M: реплей тейка 5m vs 30m по историческим входам")
    p.add_argument("--days", type=int, default=7, help="Глубина выборки BUY (календарных дней)")
    p.add_argument("--exchange", type=str, default="US", help="Код биржи в market_bars_*")
    p.add_argument("--no-db-bars", action="store_true", help="Не читать market_bars_* — только Yahoo")
    p.add_argument("--json-out", type=str, default="", help="Путь к JSON-отчёту")
    p.add_argument(
        "--full-30m-sim",
        action="store_true",
        help="Эмуляция полной 30m-стратегии на окне --days ET (см. --sim-30m-only без реплея по BUY)",
    )
    p.add_argument(
        "--sim-30m-only",
        action="store_true",
        help="Только полная 30m-эмуляция: не делать реплей выходов 5m/30m по входам из trade_history. "
        "Тикеры: --tickers или GAME_5M_TICKERS / TICKERS_FAST (get_tickers_game_5m). Требует --full-30m-sim.",
    )
    p.add_argument(
        "--tickers",
        type=str,
        default="",
        help="Список тикеров через запятую (для --sim-30m-only; иначе игнорируется)",
    )
    p.add_argument(
        "--no-kb-on-30m-sim",
        action="store_true",
        help="В --full-30m-sim не применять KB/VIX после технического сигнала (чисто техническая эмуляция)",
    )
    p.add_argument(
        "--sim-30m-kb-days",
        type=int,
        default=14,
        metavar="N",
        help="Глубность загрузки KB (дней) для 30m-эмуляции; фактически max(N, длина окна+запас)",
    )
    args = p.parse_args()

    if args.sim_30m_only and not args.full_30m_sim:
        print("Ошибка: --sim-30m-only требует --full-30m-sim.")
        sys.exit(2)

    engine = create_engine(get_database_url())
    bar_engine = None if args.no_db_bars else engine

    buys: list[dict[str, Any]] = []
    if not args.sim_30m_only:
        buys = _fetch_buys(engine, max(1, args.days))
        if not buys:
            print(f"Нет BUY GAME_5M за последние {args.days} дн.")
            return

    results: list[dict[str, Any]] = []
    for b in buys:
        buy_id = int(b["id"])
        ticker = str(b["ticker"]).strip().upper()
        entry_price = float(b["price"])
        entry_ts = b["ts"]
        entry_et = pd.Timestamp(trade_ts_to_et(entry_ts))

        start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=8)).floor("s")
        end_utc = (entry_et.tz_convert("UTC") + pd.Timedelta(days=6)).ceil("s")

        df5 = load_bars_5m_for_replay(bar_engine, ticker, args.exchange, start_utc, end_utc)
        df30 = load_bars_30m_for_replay(bar_engine, ticker, args.exchange, start_utc, end_utc)

        ex5 = replay_game5m_on_bars(
            df5,
            entry_ts_et=entry_et,
            entry_price=entry_price,
            ticker=ticker,
            bar_minutes=5,
            momentum_fn=partial(momentum_from_5m_slice, ticker=ticker),
        )
        ex30 = replay_game5m_on_bars(
            df30,
            entry_ts_et=entry_et,
            entry_price=entry_price,
            ticker=ticker,
            bar_minutes=30,
            momentum_fn=partial(momentum_from_30m_slice, ticker=ticker),
        )

        act = _fetch_next_sell(engine, ticker, entry_ts, buy_id)

        row: dict[str, Any] = {
            "buy_id": buy_id,
            "ticker": ticker,
            "entry_ts": str(entry_ts),
            "entry_price": entry_price,
            "bars_5m_loaded": len(df5),
            "bars_30m_loaded": len(df30),
            "replay_5m": None
            if ex5 is None
            else {
                "signal_type": ex5.signal_type,
                "exit_detail": ex5.exit_detail,
                "bar_end_et": str(ex5.bar_end_et),
                "exit_fill_price": round(ex5.exit_fill_price, 6),
                "momentum_2h_pct": None if ex5.momentum_2h_pct is None else round(float(ex5.momentum_2h_pct), 4),
                "take_pct_effective": None if ex5.take_pct_effective is None else round(float(ex5.take_pct_effective), 4),
                "log_ret": log_return_pnl(entry_price, ex5.exit_fill_price),
            },
            "replay_30m": None
            if ex30 is None
            else {
                "signal_type": ex30.signal_type,
                "exit_detail": ex30.exit_detail,
                "bar_end_et": str(ex30.bar_end_et),
                "exit_fill_price": round(ex30.exit_fill_price, 6),
                "momentum_2h_pct": None if ex30.momentum_2h_pct is None else round(float(ex30.momentum_2h_pct), 4),
                "take_pct_effective": None if ex30.take_pct_effective is None else round(float(ex30.take_pct_effective), 4),
                "log_ret": log_return_pnl(entry_price, ex30.exit_fill_price),
            },
            "actual_sell": None
            if act is None
            else {
                "id": act["id"],
                "ts": str(act["ts"]),
                "price": float(act["price"]),
                "signal_type": act.get("signal_type"),
                "log_ret": log_return_pnl(entry_price, float(act["price"])),
            },
        }

        r5 = row["replay_5m"]
        r3 = row["replay_30m"]
        if r5 and r3 and r5.get("log_ret") is not None and r3.get("log_ret") is not None:
            row["log_ret_diff_5m_minus_30m"] = round(float(r5["log_ret"]) - float(r3["log_ret"]), 6)

        results.append(row)

    n = len(results)
    if not args.sim_30m_only:
        both = sum(1 for r in results if r["replay_5m"] and r["replay_30m"])
        print(f"BUY за период: {n}; оба реплея нашли выход: {both}")
        for r in results[:20]:
            tag = f"{r['ticker']} buy_id={r['buy_id']}"
            a = r["replay_5m"]
            b = r["replay_30m"]
            if a and b:
                d = r.get("log_ret_diff_5m_minus_30m")
                print(
                    f"  {tag}: 5m {a['signal_type']} @ {a['bar_end_et']} log_ret={a['log_ret']:.5f} | "
                    f"30m {b['signal_type']} @ {b['bar_end_et']} log_ret={b['log_ret']:.5f} | diff={d}"
                )
            elif a:
                print(f"  {tag}: только 5m → {a['signal_type']} @ {a['bar_end_et']}")
            elif b:
                print(f"  {tag}: только 30m → {b['signal_type']} @ {b['bar_end_et']}")
            else:
                print(f"  {tag}: нет выхода в окне (мало баров или позиция не закрылась по правилам)")
        if n > 20:
            print(f"  ... ещё {n - 20} строк (полный список в JSON при --json-out)")
    else:
        print("Режим --sim-30m-only: реплей по BUY из trade_history пропущен.")

    sim_30m_by_ticker: dict[str, list[dict[str, Any]]] = {}
    window_et_meta: Optional[dict[str, str]] = None
    use_kb_30m = not bool(args.no_kb_on_30m_sim)
    tickers_for_sim: list[str] = []
    if args.full_30m_sim and args.sim_30m_only:
        raw = (args.tickers or "").strip()
        if raw:
            tickers_for_sim = sorted({t.strip().upper() for t in raw.split(",") if t.strip()})
        else:
            tickers_for_sim = sorted({t.upper() for t in get_tickers_game_5m()})
        if not tickers_for_sim:
            print("Нет тикеров: задайте --tickers A,B или GAME_5M_TICKERS / TICKERS_FAST в config.")
            return

    if args.full_30m_sim and (buys or args.sim_30m_only):
        now_et = pd.Timestamp.now(tz="America/New_York")
        w0 = now_et - pd.Timedelta(days=max(1, args.days))
        w1 = now_et
        window_et_meta = {"start": w0.isoformat(), "end": w1.isoformat()}
        start_utc = (w0.tz_convert("UTC") - pd.Timedelta(days=8)).floor("s")
        end_utc = (w1.tz_convert("UTC") + pd.Timedelta(days=1)).ceil("s")
        tickers = tickers_for_sim if args.sim_30m_only else sorted({str(b["ticker"]).strip().upper() for b in buys})
        for sym in tickers:
            df30w = load_bars_30m_for_replay(bar_engine, sym, args.exchange, start_utc, end_utc)
            sim_30m_by_ticker[sym] = simulate_game5m_30m_strategy_on_window(
                df30w,
                sym,
                window_start_et=w0,
                window_end_et=w1,
                use_kb=use_kb_30m,
                kb_days=max(1, int(args.sim_30m_kb_days)),
            )
        total_sim = sum(len(v) for v in sim_30m_by_ticker.values())
        print(f"Полная 30m-эмуляция на [{w0.date()} .. {w1.date()}] ET: сделок всего {total_sim} по тикерам {', '.join(tickers)}")
        for sym, trs in sim_30m_by_ticker.items():
            for t in trs[:3]:
                print(
                    f"  [{sym}] sim: in {t.get('entry_ts')} @ {t.get('entry_price'):.2f} "
                    f"→ out {t.get('exit_ts')} {t.get('exit_signal')} @ {t.get('exit_fill_price'):.2f} "
                    f"log_ret={t.get('log_ret')}"
                )
            if len(trs) > 3:
                print(f"  [{sym}] ... ещё {len(trs) - 3} сделок")

    if args.json_out.strip():
        out_path = Path(args.json_out.strip())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "exchange": args.exchange,
            "rows": results,
            "sim_30m_only": bool(args.sim_30m_only),
        }
        if args.full_30m_sim:
            payload["full_30m_strategy_sim"] = sim_30m_by_ticker
            payload["full_30m_window_et"] = window_et_meta or {}
            payload["full_30m_sim_use_kb"] = use_kb_30m
            payload["full_30m_sim_kb_days_arg"] = max(1, int(args.sim_30m_kb_days))
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {out_path.resolve()}")


if __name__ == "__main__":
    main()

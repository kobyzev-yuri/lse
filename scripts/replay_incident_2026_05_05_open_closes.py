#!/usr/bin/env python3
"""
Replay the "09:25 close" incident on 2026-05-05 using bar-based simulation.

Goal:
- For a list of tickers, load the actual BUY legs + the incident SELL from trade_history (GAME_5M)
- Load 5m bars for the period (DB market_bars_5m if available, else yfinance)
- Restrict bars to regular session only (>= 09:30 ET) for 2026-05-05
- Run replay_game5m_on_bars() with simulation_time=bar_end_et
- Print the expected exit (time, reason, price) vs the recorded incident exit

This is intentionally an offline analysis tool (no writes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text

from config_loader import get_database_url
from services.game_5m import trade_ts_to_et
from services.game_5m_take_replay import load_bars_5m_for_replay, replay_game5m_on_bars, momentum_from_5m_slice


@dataclass
class IncidentTrade:
    ticker: str
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts_db: pd.Timestamp
    exit_price_db: float
    exit_signal_db: str
    exit_ctx: Optional[dict[str, Any]]


def _ctx_dict(raw: Any) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _load_incident(engine, ticker: str) -> Optional[IncidentTrade]:
    """
    Pick the last BUY before the incident SELL on 2026-05-05 16:25 MSK,
    and the SELL itself (TIME_EXIT_EARLY/TAKE_PROFIT* recorded at 16:25 MSK).
    """
    rows = engine.execute(
        text(
            """
            SELECT id, ts, side, price, quantity, signal_type, ts_timezone, context_json
            FROM public.trade_history
            WHERE strategy_name='GAME_5M'
              AND UPPER(TRIM(ticker)) = :t
              AND ts >= '2026-05-04'::timestamp AND ts < '2026-05-06'::timestamp
            ORDER BY ts ASC, id ASC
            """
        ),
        {"t": ticker.upper()},
    ).fetchall()
    if not rows:
        return None

    # Find the incident SELL at 2026-05-05 16:25 MSK (ts stored as naive MSK wall time)
    incident_sell = None
    for r in rows:
        if r[2] == "SELL" and str(r[1]).startswith("2026-05-05 16:25"):
            incident_sell = r
            break
    if incident_sell is None:
        return None

    sell_ts = pd.Timestamp(incident_sell[1])
    sell_px = float(incident_sell[3])
    sell_sig = str(incident_sell[5] or "")
    sell_ctx = _ctx_dict(incident_sell[7])

    # Last BUY before this SELL
    last_buy = None
    for r in rows:
        if r[2] == "BUY" and pd.Timestamp(r[1]) <= sell_ts:
            last_buy = r
    if last_buy is None:
        return None
    buy_ts = pd.Timestamp(last_buy[1])
    buy_px = float(last_buy[3])

    # Convert entry ts to ET for replay start
    buy_ts_et = pd.Timestamp(trade_ts_to_et(buy_ts, source_tz=last_buy[6] or "Europe/Moscow"))

    return IncidentTrade(
        ticker=ticker.upper(),
        entry_ts=buy_ts_et,
        entry_price=buy_px,
        exit_ts_db=sell_ts,
        exit_price_db=sell_px,
        exit_signal_db=sell_sig,
        exit_ctx=sell_ctx,
    )


def _filter_rth_only(df: pd.DataFrame, day: str) -> pd.DataFrame:
    """Keep only regular session bars for `day` in ET: [09:30, 16:00]."""
    if df is None or df.empty:
        return pd.DataFrame()
    d = pd.to_datetime(df["datetime"], errors="coerce")
    if d.dt.tz is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        d = d.dt.tz_convert("America/New_York")
    out = df.copy()
    out["datetime"] = d
    d0 = pd.Timestamp(f"{day} 09:30", tz="America/New_York")
    d1 = pd.Timestamp(f"{day} 16:00", tz="America/New_York")
    m = (out["datetime"] >= d0) & (out["datetime"] <= d1)
    return out.loc[m].sort_values("datetime").reset_index(drop=True)


def main():
    tickers = ["MU", "NBIS", "ASML", "SNDK"]
    db_url = get_database_url()
    engine = create_engine(db_url)

    print("Replay incident 2026-05-05 (RTH bars only)")
    print("DB:", db_url.split("@")[-1] if "@" in db_url else db_url)
    print()

    # Replay bars window: wide enough to cover the day and momentum lookback (2h)
    start_utc = pd.Timestamp("2026-05-05 00:00:00+00:00")
    end_utc = pd.Timestamp("2026-05-06 00:00:00+00:00")

    with engine.connect() as conn:
        for t in tickers:
            inc = _load_incident(conn, t)
            if not inc:
                print(t, ": no incident SELL @ 16:25 MSK found")
                continue

            df5 = load_bars_5m_for_replay(engine, ticker=t, exchange="US", start_utc=start_utc, end_utc=end_utc)
            df5_rth = _filter_rth_only(df5, "2026-05-05")
            if df5_rth.empty:
                print(t, ": no RTH 5m bars available (DB empty and Yahoo returned empty?)")
                continue

            rep = replay_game5m_on_bars(
                df5_rth,
                entry_ts_et=inc.entry_ts,
                entry_price=float(inc.entry_price),
                ticker=t,
                bar_minutes=5,
                momentum_fn=lambda sl: momentum_from_5m_slice(sl, ticker=t),
            )

            # Recorded bar window in context_json, if any
            ctx = inc.exit_ctx or {}
            bar0 = ctx.get("exit_bar_start_et")
            bar1 = ctx.get("exit_bar_end_et")

            print("=" * 80)
            print(f"{t}: recorded SELL ts(MSK)={inc.exit_ts_db} px={inc.exit_price_db:.4f} signal={inc.exit_signal_db}")
            if bar0 or bar1:
                print(f"    recorded window ET: [{bar0} .. {bar1})")
            print(f"    entry ET: {inc.entry_ts} @ {inc.entry_price:.4f}")
            if rep is None:
                print("    replay: NO EXIT found in RTH bars for this day")
                continue
            print(
                "    replay:",
                rep.signal_type,
                rep.exit_detail or "—",
                "bar_open_et=" + rep.bar_open_et.strftime("%Y-%m-%d %H:%M"),
                "bar_end_et=" + rep.bar_end_et.strftime("%Y-%m-%d %H:%M"),
                "fill=" + f"{rep.exit_fill_price:.4f}",
                "mom2h=" + ("—" if rep.momentum_2h_pct is None else f"{rep.momentum_2h_pct:+.3f}%"),
                "take_eff=" + ("—" if rep.take_pct_effective is None else f"{rep.take_pct_effective:.3f}%"),
            )


if __name__ == "__main__":
    main()


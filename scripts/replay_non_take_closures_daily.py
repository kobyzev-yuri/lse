#!/usr/bin/env python3
"""
Replay closed trades as if auto-exit was TAKE_PROFIT-only.

This script does NOT modify DB. It builds an alternative timeline from `trade_history`
using daily `quotes` data:
  - non-TAKE SELL is ignored;
  - take level = % from BUY.take_profit at entry only (no config fallback);
  - position closes only when daily HIGH reaches that take level;
  - if take is never reached (within available quotes), position stays open.

Output:
  - concise terminal summary;
  - optional JSON report (`--json-out`).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url, get_config_value


@dataclass
class AltClosed:
    ticker: str
    entry_ts: str
    entry_price: float
    qty: float
    exit_ts: str
    exit_price: float
    exit_reason: str
    source_buy_id: int
    source_sell_id: Optional[int]


@dataclass
class AltOpen:
    ticker: str
    entry_ts: str
    entry_price: float
    qty: float
    take_pct: float
    take_level: float
    source_buy_id: int
    ignored_sells: list[int]


def _take_pct_for_buy(ticker: str, buy_take: Optional[float], allow_config_fallback: bool) -> Optional[float]:
    if buy_take is not None:
        return float(buy_take)
    if not allow_config_fallback:
        return None
    key = f"GAME_5M_TAKE_PROFIT_PCT_{(ticker or '').strip().upper()}"
    raw = (get_config_value(key, "") or "").strip()
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        return float((get_config_value("GAME_5M_TAKE_PROFIT_PCT", "7") or "7").strip())
    except (TypeError, ValueError):
        return 7.0


def _take_pct_from_context(context_json: Any) -> Optional[float]:
    if context_json is None:
        return None
    ctx = context_json
    if isinstance(ctx, str):
        s = ctx.strip()
        if not s:
            return None
        try:
            ctx = json.loads(s)
        except Exception:
            return None
    if not isinstance(ctx, dict):
        return None
    val = ctx.get("take_profit_pct")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _load_trades(engine, strategy: str) -> pd.DataFrame:
    q = text(
        """
        SELECT id, ts, ticker, side, quantity, price, signal_type, strategy_name, take_profit, context_json
        FROM trade_history
        WHERE strategy_name = :strategy
        ORDER BY ticker ASC, ts ASC, id ASC
        """
    )
    with engine.connect() as conn:
        return pd.read_sql(q, conn, params={"strategy": strategy})


def _load_quotes(engine) -> pd.DataFrame:
    q = text(
        """
        SELECT date, ticker, high, close
        FROM quotes
        WHERE high IS NOT NULL
        ORDER BY ticker ASC, date ASC
        """
    )
    with engine.connect() as conn:
        return pd.read_sql(q, conn)


def _first_take_hit(quotes_tkr: pd.DataFrame, from_ts: pd.Timestamp, take_level: float) -> Optional[tuple[pd.Timestamp, float]]:
    if quotes_tkr.empty:
        return None
    q = quotes_tkr.copy()
    q["date"] = pd.to_datetime(q["date"])
    q = q[q["date"] >= from_ts]
    if q.empty:
        return None
    hit = q[q["high"] >= take_level]
    if hit.empty:
        return None
    row = hit.iloc[0]
    return pd.Timestamp(row["date"]), float(take_level)


def build_alt_history(trades: pd.DataFrame, quotes: pd.DataFrame) -> dict[str, Any]:
    closed: list[AltClosed] = []
    open_positions: list[AltOpen] = []
    stats = {
        "buy_total": 0,
        "sell_total": 0,
        "sell_take_total": 0,
        "sell_non_take_total": 0,
        "ignored_non_take_sells": 0,
        "ignored_buys_while_open": 0,
        "skipped_buys_without_take_at_entry": 0,
    }

    for ticker, g in trades.groupby("ticker", sort=True):
        g = g.sort_values(["ts", "id"]).reset_index(drop=True)
        q_tkr = quotes[quotes["ticker"] == ticker].copy()
        q_tkr["date"] = pd.to_datetime(q_tkr["date"])

        pos = None
        for _, row in g.iterrows():
            ts = pd.Timestamp(row["ts"])
            side = str(row["side"] or "").upper()
            signal = str(row.get("signal_type") or "").upper()
            rid = int(row["id"])

            # Before each event, check if pending position already reached take by this event timestamp.
            if pos is not None:
                hit = _first_take_hit(q_tkr, pos["last_check_ts"], pos["take_level"])
                if hit is not None and hit[0] <= ts:
                    closed.append(
                        AltClosed(
                            ticker=ticker,
                            entry_ts=str(pos["entry_ts"]),
                            entry_price=float(pos["entry_price"]),
                            qty=float(pos["qty"]),
                            exit_ts=str(hit[0]),
                            exit_price=float(hit[1]),
                            exit_reason="TAKE_PROFIT_REPLAY",
                            source_buy_id=int(pos["buy_id"]),
                            source_sell_id=None,
                        )
                    )
                    pos = None

            if side == "BUY":
                stats["buy_total"] += 1
                if pos is not None:
                    stats["ignored_buys_while_open"] += 1
                    continue
                price = float(row["price"])
                qty = float(row["quantity"])
                buy_take = None
                if "take_profit" in row and pd.notna(row["take_profit"]):
                    buy_take = float(row["take_profit"])
                if buy_take is None:
                    buy_take = _take_pct_from_context(row.get("context_json"))
                take_pct = _take_pct_for_buy(
                    ticker,
                    buy_take,
                    allow_config_fallback=False,
                )
                if take_pct is None:
                    stats["skipped_buys_without_take_at_entry"] += 1
                    continue
                pos = {
                    "buy_id": rid,
                    "entry_ts": ts,
                    "entry_price": price,
                    "qty": qty,
                    "take_pct": take_pct,
                    "take_level": price * (1.0 + take_pct / 100.0),
                    "last_check_ts": ts,
                    "ignored_sells": [],
                }
                continue

            if side == "SELL":
                stats["sell_total"] += 1
                if signal == "TAKE_PROFIT":
                    stats["sell_take_total"] += 1
                else:
                    stats["sell_non_take_total"] += 1
                if pos is None:
                    continue
                if signal == "TAKE_PROFIT":
                    closed.append(
                        AltClosed(
                            ticker=ticker,
                            entry_ts=str(pos["entry_ts"]),
                            entry_price=float(pos["entry_price"]),
                            qty=float(pos["qty"]),
                            exit_ts=str(ts),
                            exit_price=float(row["price"]),
                            exit_reason="TAKE_PROFIT_FACT",
                            source_buy_id=int(pos["buy_id"]),
                            source_sell_id=rid,
                        )
                    )
                    pos = None
                else:
                    stats["ignored_non_take_sells"] += 1
                    pos["ignored_sells"].append(rid)
                    pos["last_check_ts"] = ts

        # End-of-history take check for still-open position.
        if pos is not None:
            hit = _first_take_hit(q_tkr, pos["last_check_ts"], pos["take_level"])
            if hit is not None:
                closed.append(
                    AltClosed(
                        ticker=ticker,
                        entry_ts=str(pos["entry_ts"]),
                        entry_price=float(pos["entry_price"]),
                        qty=float(pos["qty"]),
                        exit_ts=str(hit[0]),
                        exit_price=float(hit[1]),
                        exit_reason="TAKE_PROFIT_REPLAY",
                        source_buy_id=int(pos["buy_id"]),
                        source_sell_id=None,
                    )
                )
            else:
                open_positions.append(
                    AltOpen(
                        ticker=ticker,
                        entry_ts=str(pos["entry_ts"]),
                        entry_price=float(pos["entry_price"]),
                        qty=float(pos["qty"]),
                        take_pct=float(pos["take_pct"]),
                        take_level=float(pos["take_level"]),
                        source_buy_id=int(pos["buy_id"]),
                        ignored_sells=[int(x) for x in pos["ignored_sells"]],
                    )
                )

    return {
        "stats": stats,
        "closed": [asdict(x) for x in closed],
        "open": [asdict(x) for x in open_positions],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Replay non-TAKE closures with daily quotes")
    p.add_argument("--strategy", default="GAME_5M", help="strategy_name filter (default: GAME_5M)")
    p.add_argument("--json-out", default="local/replay_non_take_take_only.json", help="where to save JSON")
    args = p.parse_args()

    engine = create_engine(get_database_url())
    trades = _load_trades(engine, args.strategy)
    if trades.empty:
        print(f"No trades for strategy={args.strategy}")
        return
    quotes = _load_quotes(engine)
    payload = build_alt_history(trades, quotes)

    stats = payload["stats"]
    print(f"Strategy: {args.strategy}")
    print(f"BUY total: {stats['buy_total']}")
    print(f"SELL total: {stats['sell_total']} (take={stats['sell_take_total']}, non_take={stats['sell_non_take_total']})")
    print(f"Ignored non-TAKE sells: {stats['ignored_non_take_sells']}")
    print(f"Ignored BUY while position still open: {stats['ignored_buys_while_open']}")
    print(f"Skipped BUY without take at entry: {stats['skipped_buys_without_take_at_entry']}")
    print(f"Alternative closed positions: {len(payload['closed'])}")
    print(f"Alternative open positions: {len(payload['open'])}")

    out = Path(args.json_out)
    if not out.is_absolute():
        out = project_root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved report: {out}")


if __name__ == "__main__":
    main()


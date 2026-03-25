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


def _pnl_pct(entry: float, exitp: float) -> Optional[float]:
    try:
        if entry and entry > 0 and exitp and exitp > 0:
            return (exitp - entry) / entry * 100.0
    except Exception:
        pass
    return None


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


def _ctx_dict(context_json: Any) -> Optional[dict[str, Any]]:
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
    return ctx


def _take_pct_from_context(context_json: Any) -> Optional[float]:
    ctx = _ctx_dict(context_json)
    if ctx is None:
        return None
    # Приоритет: эффективная/оценочная цель (с учётом тикера), затем базовый take_profit_pct.
    for key in ("effective_take_profit_pct", "estimated_upside_pct_day", "take_profit_pct"):
        val = ctx.get(key)
        if val is None:
            continue
        try:
            v = float(val)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def _adaptive_take_pct_from_context(context_json: Any) -> Optional[float]:
    """
    Новый алгоритм цели (take %) по прогнозам 30/60/120м из context_json.price_forecast_5m.
    Берём взвешенный p50 и ограничиваем его «достижимым» потолком через p90.
    """
    ctx = _ctx_dict(context_json)
    if ctx is None:
        return None
    fc = ctx.get("price_forecast_5m")
    if not isinstance(fc, dict):
        return None
    horizons = fc.get("horizons")
    if not isinstance(horizons, list) or not horizons:
        return None

    p50_by_m: dict[int, float] = {}
    p90_by_m: dict[int, float] = {}
    for h in horizons:
        if not isinstance(h, dict):
            continue
        try:
            m = int(h.get("minutes"))
        except (TypeError, ValueError):
            continue
        try:
            p50 = float(h.get("p50_pct_vs_spot"))
            if p50 > 0:
                p50_by_m[m] = p50
        except (TypeError, ValueError):
            pass
        try:
            p90 = float(h.get("p90_pct_vs_spot"))
            if p90 > 0:
                p90_by_m[m] = p90
        except (TypeError, ValueError):
            pass

    weights = {30: 0.20, 60: 0.35, 120: 0.45}
    num = 0.0
    den = 0.0
    for m, w in weights.items():
        v = p50_by_m.get(m)
        if v is None:
            continue
        num += w * v
        den += w
    if den <= 0:
        return None
    raw = num / den

    # Ограничение достижимости: не выше ~70% от p90 (если есть),
    # чтобы не ставить «идеальные» цели на хвосте распределения.
    p90_candidates = [v for k, v in p90_by_m.items() if k in (60, 120)]
    if p90_candidates:
        cap = min(p90_candidates) * 0.70
        raw = min(raw, cap)

    # Рабочий диапазон для 5m игры (консервативнее, чем жесткие 7%).
    return max(1.5, min(7.0, raw))


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


def build_alt_history(
    trades: pd.DataFrame,
    quotes: pd.DataFrame,
    target_mode: str = "legacy",
    eligible_only: bool = False,
) -> dict[str, Any]:
    closed: list[AltClosed] = []
    open_positions: list[AltOpen] = []
    stats = {
        "buy_total": 0,
        "buy_considered": 0,
        "sell_total": 0,
        "sell_take_total": 0,
        "sell_non_take_total": 0,
        "ignored_non_take_sells": 0,
        "ignored_buys_while_open": 0,
        "skipped_buys_without_take_at_entry": 0,
    }
    comparisons: list[dict[str, Any]] = []

    for ticker, g in trades.groupby("ticker", sort=True):
        g = g.sort_values(["ts", "id"]).reset_index(drop=True)
        q_tkr = quotes[quotes["ticker"] == ticker].copy()
        q_tkr["date"] = pd.to_datetime(q_tkr["date"])

        pos = None
        pending_orig: Optional[dict[str, Any]] = None
        for _, row in g.iterrows():
            ts = pd.Timestamp(row["ts"])
            side = str(row["side"] or "").upper()
            signal = str(row.get("signal_type") or "").upper()
            rid = int(row["id"])

            # Before each event, check if pending position already reached take by this event timestamp.
            if pos is not None:
                hit = _first_take_hit(q_tkr, pos["last_check_ts"], pos["take_level"])
                if hit is not None and hit[0] <= ts:
                    # альтернативное закрытие по тейку (до текущего события)
                    alt = AltClosed(
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
                    closed.append(
                        alt
                    )
                    if pending_orig is not None and pending_orig.get("buy_id") == int(pos["buy_id"]):
                        comparisons.append({
                            "ticker": ticker,
                            "buy_id": int(pos["buy_id"]),
                            "buy_ts": str(pos["entry_ts"]),
                            "buy_price": float(pos["entry_price"]),
                            "qty": float(pos["qty"]),
                            "orig_sell_id": pending_orig.get("sell_id"),
                            "orig_exit_ts": pending_orig.get("sell_ts"),
                            "orig_exit_price": pending_orig.get("sell_price"),
                            "orig_exit_reason": pending_orig.get("sell_reason"),
                            "orig_pnl_pct": pending_orig.get("sell_pnl_pct"),
                            "alt_status": "CLOSED",
                            "alt_exit_ts": alt.exit_ts,
                            "alt_exit_price": alt.exit_price,
                            "alt_exit_reason": alt.exit_reason,
                            "alt_pnl_pct": _pnl_pct(float(pos["entry_price"]), alt.exit_price),
                            "ignored_non_take_sell_ids": list(pos.get("ignored_sells") or []),
                        })
                        pending_orig = None
                    pos = None

            if side == "BUY":
                stats["buy_total"] += 1
                if pos is not None:
                    # В replay-модели "входы независимы": новый BUY начинается как новый кейс.
                    # Предыдущий кейс фиксируем как OPEN, если к этому моменту тейк не достигнут.
                    comparisons.append({
                        "ticker": ticker,
                        "buy_id": int(pos["buy_id"]),
                        "buy_ts": str(pos["entry_ts"]),
                        "buy_price": float(pos["entry_price"]),
                        "qty": float(pos["qty"]),
                        "orig_sell_id": pending_orig.get("sell_id") if pending_orig else None,
                        "orig_exit_ts": pending_orig.get("sell_ts") if pending_orig else None,
                        "orig_exit_price": pending_orig.get("sell_price") if pending_orig else None,
                        "orig_exit_reason": pending_orig.get("sell_reason") if pending_orig else None,
                        "orig_pnl_pct": pending_orig.get("sell_pnl_pct") if pending_orig else None,
                        "alt_status": "OPEN",
                        "alt_exit_ts": None,
                        "alt_exit_price": None,
                        "alt_exit_reason": None,
                        "alt_pnl_pct": None,
                        "ignored_non_take_sell_ids": list(pos.get("ignored_sells") or []),
                        "alt_take_level": float(pos["take_level"]),
                        "alt_take_pct": float(pos["take_pct"]),
                    })
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
                    pos = None
                    pending_orig = None
                price = float(row["price"])
                qty = float(row["quantity"])
                adaptive_take = _adaptive_take_pct_from_context(row.get("context_json"))
                if target_mode == "adaptive":
                    take_pct = adaptive_take
                elif target_mode == "hybrid":
                    take_pct = adaptive_take
                    if take_pct is None:
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
                else:
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
                    # оригинальная сделка всё равно может иметь SELL; для сравнения пометим как SKIPPED (нет исторического спреда входа)
                    pending_orig = {"buy_id": rid, "buy_ts": str(ts), "buy_price": price, "qty": qty, "sell_id": None, "sell_ts": None, "sell_price": None, "sell_reason": None, "sell_pnl_pct": None, "skipped": True}
                    continue
                if eligible_only and adaptive_take is None:
                    stats["skipped_buys_without_take_at_entry"] += 1
                    pending_orig = {"buy_id": rid, "buy_ts": str(ts), "buy_price": price, "qty": qty, "sell_id": None, "sell_ts": None, "sell_price": None, "sell_reason": None, "sell_pnl_pct": None, "skipped": True}
                    continue
                stats["buy_considered"] += 1
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
                pending_orig = {"buy_id": rid, "buy_ts": str(ts), "buy_price": price, "qty": qty, "sell_id": None, "sell_ts": None, "sell_price": None, "sell_reason": None, "sell_pnl_pct": None, "skipped": False}
                continue

            if side == "SELL":
                stats["sell_total"] += 1
                if signal == "TAKE_PROFIT":
                    stats["sell_take_total"] += 1
                else:
                    stats["sell_non_take_total"] += 1
                if pending_orig is not None and pending_orig.get("sell_id") is None:
                    exit_price = float(row["price"]) if row.get("price") is not None else None
                    pending_orig["sell_id"] = rid
                    pending_orig["sell_ts"] = str(ts)
                    pending_orig["sell_price"] = exit_price
                    pending_orig["sell_reason"] = signal or "SELL"
                    pending_orig["sell_pnl_pct"] = _pnl_pct(float(pending_orig.get("buy_price") or 0.0), float(exit_price or 0.0))
                if pos is None:
                    # если альт-позиции не было (например skipped), но оригинал закрылся — сравнение "как было" можно всё равно отдать
                    if pending_orig is not None and pending_orig.get("skipped"):
                        comparisons.append({
                            "ticker": ticker,
                            "buy_id": int(pending_orig.get("buy_id")),
                            "buy_ts": pending_orig.get("buy_ts"),
                            "buy_price": float(pending_orig.get("buy_price") or 0.0),
                            "qty": float(pending_orig.get("qty") or 0.0),
                            "orig_sell_id": pending_orig.get("sell_id"),
                            "orig_exit_ts": pending_orig.get("sell_ts"),
                            "orig_exit_price": pending_orig.get("sell_price"),
                            "orig_exit_reason": pending_orig.get("sell_reason"),
                            "orig_pnl_pct": pending_orig.get("sell_pnl_pct"),
                            "alt_status": "SKIPPED_NO_TAKE_AT_ENTRY",
                            "alt_exit_ts": None,
                            "alt_exit_price": None,
                            "alt_exit_reason": None,
                            "alt_pnl_pct": None,
                            "ignored_non_take_sell_ids": [],
                        })
                        pending_orig = None
                    continue
                if signal == "TAKE_PROFIT":
                    alt = AltClosed(
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
                    closed.append(
                        alt
                    )
                    if pending_orig is not None and pending_orig.get("buy_id") == int(pos["buy_id"]):
                        comparisons.append({
                            "ticker": ticker,
                            "buy_id": int(pos["buy_id"]),
                            "buy_ts": str(pos["entry_ts"]),
                            "buy_price": float(pos["entry_price"]),
                            "qty": float(pos["qty"]),
                            "orig_sell_id": pending_orig.get("sell_id"),
                            "orig_exit_ts": pending_orig.get("sell_ts"),
                            "orig_exit_price": pending_orig.get("sell_price"),
                            "orig_exit_reason": pending_orig.get("sell_reason"),
                            "orig_pnl_pct": pending_orig.get("sell_pnl_pct"),
                            "alt_status": "CLOSED",
                            "alt_exit_ts": alt.exit_ts,
                            "alt_exit_price": alt.exit_price,
                            "alt_exit_reason": alt.exit_reason,
                            "alt_pnl_pct": _pnl_pct(float(pos["entry_price"]), alt.exit_price),
                            "ignored_non_take_sell_ids": list(pos.get("ignored_sells") or []),
                        })
                        pending_orig = None
                    pos = None
                else:
                    stats["ignored_non_take_sells"] += 1
                    pos["ignored_sells"].append(rid)
                    pos["last_check_ts"] = ts

        # End-of-history take check for still-open position.
        if pos is not None:
            hit = _first_take_hit(q_tkr, pos["last_check_ts"], pos["take_level"])
            if hit is not None:
                alt = AltClosed(
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
                closed.append(
                    alt
                )
                if pending_orig is not None and pending_orig.get("buy_id") == int(pos["buy_id"]):
                    comparisons.append({
                        "ticker": ticker,
                        "buy_id": int(pos["buy_id"]),
                        "buy_ts": str(pos["entry_ts"]),
                        "buy_price": float(pos["entry_price"]),
                        "qty": float(pos["qty"]),
                        "orig_sell_id": pending_orig.get("sell_id"),
                        "orig_exit_ts": pending_orig.get("sell_ts"),
                        "orig_exit_price": pending_orig.get("sell_price"),
                        "orig_exit_reason": pending_orig.get("sell_reason"),
                        "orig_pnl_pct": pending_orig.get("sell_pnl_pct"),
                        "alt_status": "CLOSED",
                        "alt_exit_ts": alt.exit_ts,
                        "alt_exit_price": alt.exit_price,
                        "alt_exit_reason": alt.exit_reason,
                        "alt_pnl_pct": _pnl_pct(float(pos["entry_price"]), alt.exit_price),
                        "ignored_non_take_sell_ids": list(pos.get("ignored_sells") or []),
                    })
                    pending_orig = None
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
                if pending_orig is not None and pending_orig.get("buy_id") == int(pos["buy_id"]):
                    comparisons.append({
                        "ticker": ticker,
                        "buy_id": int(pos["buy_id"]),
                        "buy_ts": str(pos["entry_ts"]),
                        "buy_price": float(pos["entry_price"]),
                        "qty": float(pos["qty"]),
                        "orig_sell_id": pending_orig.get("sell_id"),
                        "orig_exit_ts": pending_orig.get("sell_ts"),
                        "orig_exit_price": pending_orig.get("sell_price"),
                        "orig_exit_reason": pending_orig.get("sell_reason"),
                        "orig_pnl_pct": pending_orig.get("sell_pnl_pct"),
                        "alt_status": "OPEN",
                        "alt_exit_ts": None,
                        "alt_exit_price": None,
                        "alt_exit_reason": None,
                        "alt_pnl_pct": None,
                        "ignored_non_take_sell_ids": list(pos.get("ignored_sells") or []),
                        "alt_take_level": float(pos["take_level"]),
                        "alt_take_pct": float(pos["take_pct"]),
                    })
                    pending_orig = None

    return {
        "target_mode": target_mode,
        "eligible_only": bool(eligible_only),
        "stats": stats,
        "closed": [asdict(x) for x in closed],
        "open": [asdict(x) for x in open_positions],
        "comparisons": comparisons,
        "blocked_entries": [],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Replay non-TAKE closures with daily quotes")
    p.add_argument("--strategy", default="GAME_5M", help="strategy_name filter (default: GAME_5M)")
    p.add_argument("--target-mode", default="legacy", choices=["legacy", "adaptive", "hybrid"], help="legacy: take as saved at entry; adaptive: target from 30/60/120 forecast only; hybrid: adaptive if available else legacy")
    p.add_argument("--eligible-only", action="store_true", help="consider only buys with adaptive-forecast context (for fair retrospective subset)")
    p.add_argument("--json-out", default="local/replay_non_take_take_only.json", help="where to save JSON")
    args = p.parse_args()

    engine = create_engine(get_database_url())
    trades = _load_trades(engine, args.strategy)
    if trades.empty:
        print(f"No trades for strategy={args.strategy}")
        return
    quotes = _load_quotes(engine)
    payload = build_alt_history(
        trades,
        quotes,
        target_mode=args.target_mode,
        eligible_only=bool(args.eligible_only),
    )

    stats = payload["stats"]
    print(f"Strategy: {args.strategy}")
    print(f"Target mode: {args.target_mode}")
    print(f"Eligible only: {bool(args.eligible_only)}")
    print(f"BUY total: {stats['buy_total']}")
    print(f"BUY considered: {stats['buy_considered']}")
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


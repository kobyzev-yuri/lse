#!/usr/bin/env python3
"""
Исправить signal_type и exit_* в context_json SELL (post-hoc).

Пример (ложный TAKE_PROFIT по wick при минусе по close):
  python scripts/correct_trade_exit_signal.py --trade-id 705 \\
    --signal-type TIME_EXIT_EARLY --exit-detail early_derisk --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text

from config_loader import get_database_url
from services.recommend_5m import build_5m_trade_close_narrative


def _load_sell_row(conn, trade_id: int) -> dict | None:
    row = conn.execute(
        text("""
            SELECT id, ticker, side, price, quantity, signal_type, strategy_name, context_json
            FROM trade_history WHERE id = :id
        """),
        {"id": trade_id},
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "ticker": row[1],
        "side": row[2],
        "price": float(row[3]),
        "quantity": float(row[4]),
        "signal_type": row[5],
        "strategy_name": row[6],
        "context_json": row[7] if row[7] is not None else {},
    }


def _last_buy_context(conn, ticker: str, strategy_name: str, before_id: int) -> dict | None:
    row = conn.execute(
        text("""
            SELECT context_json FROM trade_history
            WHERE UPPER(TRIM(ticker)) = UPPER(TRIM(:ticker))
              AND side = 'BUY' AND id < :sell_id
              AND TRIM(COALESCE(strategy_name, '')) = TRIM(:strategy)
            ORDER BY id DESC LIMIT 1
        """),
        {"ticker": ticker, "sell_id": before_id, "strategy": strategy_name or ""},
    ).fetchone()
    if not row or row[0] is None:
        return None
    ctx = row[0]
    return ctx if isinstance(ctx, dict) else json.loads(ctx)


def main() -> int:
    ap = argparse.ArgumentParser(description="Correct trade_history SELL signal_type / context_json exit fields.")
    ap.add_argument("--trade-id", type=int, required=True)
    ap.add_argument("--signal-type", required=True, help="e.g. TIME_EXIT_EARLY")
    ap.add_argument("--exit-detail", default="", help="e.g. early_derisk, stale_reversal")
    ap.add_argument("--reason", default="manual_correction_false_take_wick", help="stored in exit_correction.reason")
    ap.add_argument("--apply", action="store_true", help="Write to DB (default: dry-run)")
    args = ap.parse_args()

    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        sell = _load_sell_row(conn, args.trade_id)
        if not sell:
            print(f"trade_id={args.trade_id}: not found")
            return 1
        if (sell["side"] or "").upper() != "SELL":
            print(f"trade_id={args.trade_id}: not a SELL row")
            return 1

        buy_ctx = _last_buy_context(
            conn, sell["ticker"], sell["strategy_name"] or "", args.trade_id
        )
        buy_row = conn.execute(
            text("""
                SELECT price FROM trade_history
                WHERE UPPER(TRIM(ticker)) = UPPER(TRIM(:ticker))
                  AND side = 'BUY' AND id < :sell_id
                  AND TRIM(COALESCE(strategy_name, '')) = TRIM(:strategy)
                ORDER BY id DESC LIMIT 1
            """),
            {
                "ticker": sell["ticker"],
                "sell_id": args.trade_id,
                "strategy": (sell["strategy_name"] or "").strip(),
            },
        ).fetchone()
        entry_price = float(buy_row[0]) if buy_row and buy_row[0] else 0.0

        ctx = dict(sell["context_json"]) if isinstance(sell["context_json"], dict) else {}
        old_signal = sell["signal_type"]
        old_exit_signal = ctx.get("exit_signal")

        narrative = build_5m_trade_close_narrative(
            exit_type=args.signal_type,
            exit_detail=args.exit_detail,
            entry_price=entry_price,
            exit_price=sell["price"],
            take_pct=4.5,
            stop_pct=0.0,
            entry_ctx=buy_ctx,
            exit_reasoning_excerpt=(ctx.get("reasoning") or "")[:200],
        )
        ctx.update(narrative)
        if args.exit_detail:
            ctx["exit_detail"] = args.exit_detail
        ctx["exit_correction"] = {
            "corrected_at_utc": datetime.now(timezone.utc).isoformat(),
            "from_signal_type": old_signal,
            "from_exit_signal": old_exit_signal,
            "reason": args.reason,
            "note": (
                "Переклассификация: TAKE_PROFIT по bar_high при отрицательном close; "
                "с новой логикой — TIME_EXIT_EARLY до проверки тейка."
            ),
        }

        print(
            f"trade_id={args.trade_id} {sell['ticker']} "
            f"{old_signal} -> {args.signal_type}"
            f"{(' / ' + args.exit_detail) if args.exit_detail else ''}"
        )
        print(f"  entry={entry_price:.4f} exit={sell['price']:.4f}")
        print(f"  exit_condition: {ctx.get('exit_condition', '')[:120]}…")

        if not args.apply:
            print("dry-run (use --apply to UPDATE)")
            return 0

        conn.execute(
            text("""
                UPDATE trade_history
                SET signal_type = :signal_type, context_json = CAST(:ctx AS jsonb)
                WHERE id = :id
            """),
            {
                "id": args.trade_id,
                "signal_type": args.signal_type,
                "ctx": json.dumps(ctx, ensure_ascii=False),
            },
        )
        conn.commit()
        print("UPDATE ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

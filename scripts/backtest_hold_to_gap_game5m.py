#!/usr/bin/env python3
"""Бэктест: удержание до open d+1/d+2/d+3 vs фактический выход GAME_5M."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.game5m_hold_to_gap_backtest import build_hold_to_gap_backtest
from services.trade_effectiveness_analyzer import (
    _estimate_trade_effects,
    _load_closed_trades,
    _prepare_ohlc_cache,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hold-to-gap counterfactual backtest (GAME_5M).")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--cost-bps", type=float, default=12.0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    closed = _load_closed_trades(days=max(1, args.days), strategy_name="GAME_5M")
    tickers = sorted({str(t.ticker) for t in closed if getattr(t, "ticker", None)})
    cache = _prepare_ohlc_cache(tickers=tickers, days=args.days + 5)
    effects = _estimate_trade_effects(closed, cache)

    from report_generator import get_engine

    report = build_hold_to_gap_backtest(
        closed,
        effects,
        cache,
        engine=get_engine(),
        limit=args.limit,
        cost_bps_roundtrip=args.cost_bps,
    )
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["days"] = args.days
    report["closed_trades"] = len(closed)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

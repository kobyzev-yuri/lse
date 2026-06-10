#!/usr/bin/env python3
"""Post-RTH GAME_5M session review for agent tuning log and daily reports.

Writes JSON: /app/logs/ml/ml_data_quality/last_game5m_daily_session_review.json
Prints Markdown summary to stdout (for cron logs / automation).

Usage (in lse-bot):
  python3 scripts/game5m_daily_session_review.py
  python3 scripts/game5m_daily_session_review.py --session-date 2026-06-10
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

OUT_PATH = Path("/app/logs/ml/ml_data_quality/last_game5m_daily_session_review.json")

TUNING_KEYS = (
    "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE",
    "GAME_5M_MULTIDAY_ENTRY_GATE_MODE",
    "GAME_5M_MULTIDAY_HOLD_GATE_MODE",
    "GAME_5M_BLOCK_NEW_BUY_NEAR_CLOSE_ENABLED",
    "GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE",
    "GAME_5M_EOD_FLATTEN_ALWAYS",
    "GAME_5M_EOD_FLATTEN_MINUTES_BEFORE_CLOSE",
)


def _parse_ctx(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _session_date_default() -> date:
    return datetime.now().date()


def _pair_trades(df) -> List[Dict[str, Any]]:
    import pandas as pd

    open_by: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        tkr = str(r.ticker).strip().upper()
        side = str(r.side).upper()
        if side == "BUY":
            open_by[tkr] = r
        elif side == "SELL" and tkr in open_by:
            b = open_by.pop(tkr)
            ep, xp = float(b.price), float(r.price)
            pnl = round((xp - ep) / ep * 100, 2) if ep else None
            bctx = _parse_ctx(b.context_json)
            ctx = _parse_ctx(r.context_json)
            exit_detail = ctx.get("exit_detail") or ""
            opened_ts = str(b.ts)
            closed_ts = str(r.ts)
            late_buy = False
            try:
                opened_dt = pd.Timestamp(b.ts)
                if opened_dt.hour >= 21 or (opened_dt.hour == 20 and opened_dt.minute >= 30):
                    late_buy = True
            except Exception:
                pass
            rows.append(
                {
                    "ticker": tkr,
                    "opened_at": opened_ts,
                    "closed_at": closed_ts,
                    "exit_type": (r.signal_type or "").strip().upper(),
                    "exit_detail": exit_detail,
                    "pnl_pct": pnl,
                    "entry_decision": bctx.get("technical_decision_effective") or bctx.get("decision"),
                    "entry_md_1d": bctx.get("multiday_lr_horizon_1d_pct_vs_spot"),
                    "entry_gate_applied": bctx.get("multiday_lr_entry_gate_applied"),
                    "late_buy_after_2030_msk": late_buy,
                }
            )
    return rows


def build_report(session_day: date) -> Dict[str, Any]:
    import pandas as pd
    from config_loader import get_config_value
    from report_generator import get_engine
    from services.multiday_lr_gate import evaluate_multiday_overnight_gate
    from services.recommend_5m import get_decision_5m
    from services.ticker_groups import get_tickers_game_5m
    from sqlalchemy import text

    t0 = time.time()
    day_s = session_day.isoformat()
    eng = get_engine()
    q = """
        SELECT id, ts, ticker, side, price, signal_type, context_json
        FROM trade_history
        WHERE strategy_name = 'GAME_5M'
          AND ts >= :d0 AND ts < :d1
        ORDER BY ts ASC, id ASC
    """
    d0 = f"{day_s} 00:00:00"
    d1 = f"{(session_day + timedelta(days=1)).isoformat()} 00:00:00"
    df = pd.read_sql(text(q), eng, params={"d0": d0, "d1": d1})
    paired = _pair_trades(df)

    by_exit: Dict[str, List[float]] = defaultdict(list)
    for p in paired:
        if p.get("pnl_pct") is not None:
            by_exit[p["exit_type"]].append(float(p["pnl_pct"]))

    time_exits = [p for p in paired if p["exit_type"] == "TIME_EXIT"]
    late_buys = [p for p in paired if p.get("late_buy_after_2030_msk")]

    config_snap = {k: get_config_value(k) for k in TUNING_KEYS}

    overnight_probe: List[Dict[str, Any]] = []
    for t in get_tickers_game_5m():
        try:
            d5 = get_decision_5m(t)
            if not d5:
                continue
            og = evaluate_multiday_overnight_gate(d5)
            overnight_probe.append(
                {
                    "ticker": t,
                    "md_1d": d5.get("multiday_lr_horizon_1d_pct_vs_spot"),
                    "would_avoid_overnight": og.get("would_avoid_overnight"),
                    "note": og.get("note"),
                }
            )
        except Exception as e:
            overnight_probe.append({"ticker": t, "error": str(e)})

    payload = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_date_msk": day_s,
        "elapsed_sec": round(time.time() - t0, 1),
        "config_snapshot": config_snap,
        "summary": {
            "closes_n": len(paired),
            "buys_n": int((df["side"].astype(str).str.upper() == "BUY").sum()) if not df.empty else 0,
            "time_exit_n": len(time_exits),
            "time_exit_early_n": sum(1 for p in paired if p["exit_type"] == "TIME_EXIT_EARLY"),
            "late_buy_after_2030_msk_n": len(late_buys),
            "avg_pnl_by_exit": {
                k: round(sum(v) / len(v), 2) if v else None for k, v in sorted(by_exit.items())
            },
            "time_exit_details": dict(Counter(p.get("exit_detail") or "(empty)" for p in time_exits)),
        },
        "trades": paired,
        "overnight_gate_probe": overnight_probe,
        "agent_log_path": "docs/GAME_5M_AGENT_TUNING_LOG.md",
    }
    return payload


def _markdown_summary(rep: Dict[str, Any]) -> str:
    s = rep.get("summary") or {}
    lines = [
        f"# GAME_5M daily review — {rep.get('session_date_msk')}",
        "",
        f"- closes: **{s.get('closes_n', 0)}** | TIME_EXIT: **{s.get('time_exit_n', 0)}** | "
        f"TIME_EXIT_EARLY: **{s.get('time_exit_early_n', 0)}** | "
        f"late BUY (≥20:30 MSK): **{s.get('late_buy_after_2030_msk_n', 0)}**",
        "",
        "**avg PnL by exit:**",
    ]
    for k, v in (s.get("avg_pnl_by_exit") or {}).items():
        lines.append(f"- {k}: {v}%")
    if s.get("time_exit_details"):
        lines.append("")
        lines.append(f"**TIME_EXIT details:** {s.get('time_exit_details')}")
    cfg = rep.get("config_snapshot") or {}
    lines.extend(
        [
            "",
            "**tuning config:**",
            f"- BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE={cfg.get('GAME_5M_BLOCK_NEW_BUY_MINUTES_BEFORE_CLOSE')}",
            f"- OVERNIGHT_GATE_MODE={cfg.get('GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE')}",
            f"- EOD_FLATTEN_ALWAYS={cfg.get('GAME_5M_EOD_FLATTEN_ALWAYS')}",
            "",
            f"JSON: `{OUT_PATH}`",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="GAME_5M post-session daily review")
    ap.add_argument("--session-date", type=str, default="", help="YYYY-MM-DD (MSK calendar day of trade_history.ts)")
    ap.add_argument("--no-write", action="store_true", help="stdout only, skip JSON file")
    args = ap.parse_args()

    if args.session_date:
        session_day = date.fromisoformat(args.session_date.strip())
    else:
        session_day = _session_date_default()

    rep = build_report(session_day)
    if not args.no_write:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(_markdown_summary(rep))
    if not args.no_write:
        print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

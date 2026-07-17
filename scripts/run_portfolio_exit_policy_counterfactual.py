#!/usr/bin/env python3
"""
Counterfactual: portfolio exit regime + late-chase thresholds.

Writes /app/logs/ml/ml_data_quality/last_portfolio_exit_policy_cf.json
(or local/logs/... outside Docker).

Examples:
  python scripts/run_portfolio_exit_policy_counterfactual.py --days 90
  docker exec lse-bot python3 /app/scripts/run_portfolio_exit_policy_counterfactual.py --days 90
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _out_path() -> Path:
    app = Path("/app/logs/ml/ml_data_quality")
    if app.is_dir():
        return app / "last_portfolio_exit_policy_cf.json"
    local = ROOT / "local" / "logs" / "ml_data_quality"
    local.mkdir(parents=True, exist_ok=True)
    return local / "last_portfolio_exit_policy_cf.json"


def _parse_ctx(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Portfolio exit/late-chase counterfactual")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--output", type=str, default="")
    args = ap.parse_args()

    from datetime import timedelta

    import pandas as pd
    from db_connection import get_engine
    from sqlalchemy import text
    from report_generator import compute_closed_trade_pnls, load_trade_history
    from services.portfolio_exit_policy import trailing_take_should_close

    engine = get_engine()
    closed = compute_closed_trade_pnls(load_trade_history(engine))
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(args.days))
    rows: List[Dict[str, Any]] = []
    for t in closed:
        es = (getattr(t, "entry_strategy", None) or "").strip().upper()
        if es == "GAME_5M":
            continue
        ts = pd.Timestamp(t.ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts < cutoff:
            continue
        entry_px = float(t.entry_price or 0.0)
        exit_px = float(t.exit_price or 0.0)
        realized = None
        if entry_px > 0 and exit_px > 0:
            realized = (exit_px / entry_px - 1.0) * 100.0
        rows.append(
            {
                "trade_id": getattr(t, "trade_id", None),
                "ticker": t.ticker,
                "entry_time": getattr(t, "entry_ts", None),
                "exit_time": t.ts,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "exit_reason": getattr(t, "signal_type", None),
                "realized_pnl_pct": realized,
                "context_json": getattr(t, "context_json", None),
                "entry_strategy": es,
            }
        )

    trailing_cases: List[Dict[str, Any]] = []
    late_chase_cf: Dict[str, Any] = {"thresholds": {}}
    n_trailing = 0
    n_would_hold_melt_up = 0
    missed_sum = 0.0
    missed_n = 0

    for row in rows:
        ctx = _parse_ctx(row.get("context_json"))
        reason = str(row.get("exit_reason") or "")
        entry_px = _f(row.get("entry_price")) or 0.0
        exit_px = _f(row.get("exit_price")) or 0.0
        realized = _f(row.get("realized_pnl_pct"))
        if realized is None and entry_px > 0 and exit_px > 0:
            realized = (exit_px / entry_px - 1.0) * 100.0
        entry_regime = str(ctx.get("portfolio_trend_regime") or "unknown")
        ret20 = _f(ctx.get("portfolio_trend_ret_20d_pct"))
        near = ctx.get("portfolio_trend_near_20d_high")
        is_trail = "TRAILING" in reason.upper() or "trailing" in reason.lower()

        if is_trail and entry_px > 0:
            n_trailing += 1
            # Approximate peak from context or realized as lower bound
            peak = _f(ctx.get("portfolio_peak_pnl_pct"))
            pnl_at_exit = realized if realized is not None else 0.0
            if peak is None and pnl_at_exit is not None:
                peak = max(pnl_at_exit, 0.0)
            tight_close, _ = trailing_take_should_close(
                float(pnl_at_exit or 0.0), peak, regime="neutral"
            )
            melt_close, _ = trailing_take_should_close(
                float(pnl_at_exit or 0.0), peak, regime="melt_up"
            )
            would_hold = bool(tight_close and not melt_close)
            if would_hold:
                n_would_hold_melt_up += 1
            # Post-exit continuation proxy: need quotes — skip if no engine path easy
            trailing_cases.append(
                {
                    "trade_id": row.get("trade_id"),
                    "ticker": row.get("ticker"),
                    "entry_regime": entry_regime,
                    "realized_pct": round(float(realized), 3) if realized is not None else None,
                    "peak_pnl_pct": peak,
                    "would_hold_under_melt_up_trailing": would_hold,
                    "exit_reason": reason[:160],
                }
            )

        # late-chase label at entry
        for thr in (18.0, 20.0, 25.0):
            key = f"ret_ge_{thr:g}"
            bucket = late_chase_cf["thresholds"].setdefault(
                key, {"n_flagged": 0, "n_loss": 0, "sum_realized": 0.0, "tickers": []}
            )
            if near and ret20 is not None and ret20 >= thr:
                bucket["n_flagged"] += 1
                if realized is not None:
                    bucket["sum_realized"] += float(realized)
                    if realized < 0:
                        bucket["n_loss"] += 1
                if len(bucket["tickers"]) < 12:
                    bucket["tickers"].append(
                        {
                            "ticker": row.get("ticker"),
                            "ret_20d": ret20,
                            "realized": realized,
                        }
                    )

    for key, bucket in late_chase_cf["thresholds"].items():
        n = int(bucket["n_flagged"])
        bucket["mean_realized_pct"] = (
            round(bucket["sum_realized"] / n, 3) if n else None
        )
        bucket["loss_rate_pct"] = (
            round(100.0 * bucket["n_loss"] / n, 1) if n else None
        )
        del bucket["sum_realized"]

    # Post-exit MFE for trailing via quotes (best-effort)
    with engine.connect() as conn:
        for case in trailing_cases[:80]:
            tid = case.get("trade_id")
            src = next((r for r in rows if r.get("trade_id") == tid), None)
            if not src or not case.get("would_hold_under_melt_up_trailing"):
                continue
            ticker = src.get("ticker")
            exit_time = src.get("exit_time")
            entry_px = _f(src.get("entry_price"))
            if not ticker or not exit_time or not entry_px:
                continue
            try:
                hi = conn.execute(
                    text(
                        """
                        SELECT MAX(high) FROM quotes
                        WHERE ticker = :t
                          AND date > CAST(:exit_ts AS date)
                          AND date <= CAST(:exit_ts AS date) + INTERVAL '20 day'
                        """
                    ),
                    {"t": ticker, "exit_ts": exit_time},
                ).fetchone()
                if hi and hi[0] is not None:
                    mfe = (float(hi[0]) / entry_px - 1.0) * 100.0
                    case["post_exit_20d_mfe_pct"] = round(mfe, 3)
                    realized = case.get("realized_pct")
                    if realized is not None:
                        missed = mfe - float(realized)
                        case["missed_upside_vs_exit_pct"] = round(missed, 3)
                        if missed > 0:
                            missed_sum += missed
                            missed_n += 1
            except Exception:
                continue

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": int(args.days),
        "n_closed_portfolio": len(rows),
        "exit_trailing": {
            "n_trailing_exits": n_trailing,
            "n_would_hold_under_melt_up_params": n_would_hold_melt_up,
            "mean_missed_upside_pct_when_would_hold": (
                round(missed_sum / missed_n, 3) if missed_n else None
            ),
            "n_with_post_exit_mfe": missed_n,
            "sample": trailing_cases[:40],
            "note_ru": (
                "would_hold = при тех же peak/pnl узкий trailing закрыл бы, melt_up — нет. "
                "Live-regime fix: на выходе брать текущий melt_up, не snapshot входа."
            ),
        },
        "late_chase": late_chase_cf,
        "verdict_hints_ru": [
            "P1: если n_would_hold>0 и mean missed_upside>0 — live melt_up trailing оправдан",
            "P2: выбрать thr late-chase с высоким loss_rate при ненулевом n_flagged",
            "5d CatBoost D1–D4 не ждать — см. PORTFOLIO_POLICY_NEXT_PLAN.md",
        ],
    }

    out = Path(args.output) if args.output else _out_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(out), "n_closed": len(rows), "n_trailing": n_trailing}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

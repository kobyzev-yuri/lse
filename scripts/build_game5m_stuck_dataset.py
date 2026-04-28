#!/usr/bin/env python3
"""
Build a supervised dataset for GAME_5M stuck-risk modeling.

Rows are closed GAME_5M trades. Labels are generated automatically from
trade_history outcomes plus 5m bars after entry:

  label_quick_win          TAKE_PROFIT within --quick-win-minutes
  label_stuck              slow/negative/forced exit or stale_reversal
  label_bad_reversal       MAE breaches --bad-reversal-mae-pct and trade does not recover
  label_recoverable_hanger not quick win, but had enough MFE to support a reduced take

Output is CSV by default:
  python scripts/build_game5m_stuck_dataset.py --out local/datasets/game5m_stuck_dataset.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import compute_closed_trade_pnls, get_engine, load_trade_history
from services.deal_params_5m import normalize_entry_context
from services.game_5m import trade_ts_to_et
from services.game_5m_take_replay import load_bars_5m_for_replay

logger = logging.getLogger(__name__)

GAME_5M = "GAME_5M"


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _pct(entry: float, price: Optional[float]) -> Optional[float]:
    if price is None or entry <= 0:
        return None
    return (float(price) / float(entry) - 1.0) * 100.0


def _ts_et(ts: Any) -> Optional[pd.Timestamp]:
    if ts is None:
        return None
    try:
        t = pd.Timestamp(trade_ts_to_et(ts))
        if t.tzinfo is None:
            t = t.tz_localize("America/New_York", ambiguous=True)
        else:
            t = t.tz_convert("America/New_York")
        return t
    except Exception:
        return None


def _context_json_excerpt(ctx: Dict[str, Any]) -> str:
    slim = {
        k: ctx.get(k)
        for k in (
            "decision",
            "technical_entry_branch",
            "entry_advice",
            "entry_strategy",
            "session_phase",
            "decision_rule_version",
        )
        if ctx.get(k) is not None
    }
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def _bars_after_entry(df5: pd.DataFrame, entry_et: pd.Timestamp, until_et: Optional[pd.Timestamp]) -> pd.DataFrame:
    if df5 is None or df5.empty:
        return pd.DataFrame()
    df = df5.copy()
    d = pd.to_datetime(df["datetime"])
    if d.dt.tz is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        d = d.dt.tz_convert("America/New_York")
    df["datetime"] = d
    m = df["datetime"] > entry_et
    if until_et is not None:
        m &= df["datetime"] <= until_et
    return df.loc[m].sort_values("datetime").reset_index(drop=True)


def _window_stats(df: pd.DataFrame, entry_price: float, minutes: Optional[int]) -> Dict[str, Any]:
    if df is None or df.empty or entry_price <= 0:
        suffix = "full" if minutes is None else f"{minutes}m"
        return {
            f"bars_{suffix}": 0,
            f"return_{suffix}_pct": None,
            f"mfe_{suffix}_pct": None,
            f"mae_{suffix}_pct": None,
            f"range_{suffix}_pct": None,
            f"volume_{suffix}": None,
        }
    sub = df
    suffix = "full" if minutes is None else f"{minutes}m"
    if minutes is not None:
        start = pd.Timestamp(df.iloc[0]["datetime"])
        end = start + pd.Timedelta(minutes=int(minutes))
        sub = df.loc[pd.to_datetime(df["datetime"]) <= end].reset_index(drop=True)
    if sub.empty:
        return {
            f"bars_{suffix}": 0,
            f"return_{suffix}_pct": None,
            f"mfe_{suffix}_pct": None,
            f"mae_{suffix}_pct": None,
            f"range_{suffix}_pct": None,
            f"volume_{suffix}": None,
        }
    close_last = _safe_float(sub.iloc[-1].get("Close"))
    high_max = _safe_float(pd.to_numeric(sub["High"], errors="coerce").max())
    low_min = _safe_float(pd.to_numeric(sub["Low"], errors="coerce").min())
    volume_sum = _safe_float(pd.to_numeric(sub.get("Volume"), errors="coerce").fillna(0).sum()) if "Volume" in sub else None
    mfe = _pct(entry_price, high_max)
    mae = _pct(entry_price, low_min)
    return {
        f"bars_{suffix}": int(len(sub)),
        f"return_{suffix}_pct": _pct(entry_price, close_last),
        f"mfe_{suffix}_pct": mfe,
        f"mae_{suffix}_pct": mae,
        f"range_{suffix}_pct": (mfe - mae) if mfe is not None and mae is not None else None,
        f"volume_{suffix}": volume_sum,
    }


def _load_trade_bars(engine: Any, ticker: str, entry_et: pd.Timestamp, exit_et: pd.Timestamp, exchange: str) -> pd.DataFrame:
    start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=2)).floor("s")
    end_utc = (exit_et.tz_convert("UTC") + pd.Timedelta(hours=2)).ceil("s")
    return load_bars_5m_for_replay(engine, ticker, exchange, start_utc, end_utc)


def _iter_dataset_rows(args: argparse.Namespace) -> Iterable[Dict[str, Any]]:
    engine = get_engine()
    trades = load_trade_history(engine, strategy_name=GAME_5M)
    closed = compute_closed_trade_pnls(trades)
    if args.days_back:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(args.days_back))
    else:
        cutoff = None

    for t in closed:
        if (getattr(t, "entry_strategy", "") or "").strip().upper() != GAME_5M:
            continue
        entry_et = _ts_et(getattr(t, "entry_ts", None))
        exit_et = _ts_et(getattr(t, "ts", None))
        if entry_et is None or exit_et is None:
            continue
        if cutoff is not None and exit_et.tz_convert("UTC") < cutoff:
            continue

        ctx = normalize_entry_context(getattr(t, "context_json", None))
        entry_price = _safe_float(getattr(t, "entry_price", None))
        exit_price = _safe_float(getattr(t, "exit_price", None))
        if entry_price is None or entry_price <= 0 or exit_price is None:
            continue

        try:
            df5 = _load_trade_bars(engine, str(t.ticker), entry_et, exit_et, args.exchange)
        except Exception as e:
            logger.warning("bars load failed trade_id=%s ticker=%s: %s", getattr(t, "trade_id", "?"), t.ticker, e)
            df5 = pd.DataFrame()
        bars = _bars_after_entry(df5, entry_et, exit_et)
        stats_30 = _window_stats(bars, entry_price, 30)
        stats_60 = _window_stats(bars, entry_price, 60)
        stats_full = _window_stats(bars, entry_price, None)

        age_minutes = max(0.0, (exit_et - entry_et).total_seconds() / 60.0)
        realized_pct = _pct(entry_price, exit_price)
        signal = (getattr(t, "signal_type", "") or "").strip().upper()
        exit_detail = ""
        try:
            exit_ctx = normalize_entry_context(getattr(t, "exit_context_json", None))
            exit_detail = str(exit_ctx.get("exit_detail") or exit_ctx.get("exit_condition") or "")[:160]
        except Exception:
            exit_detail = ""

        mfe_full = stats_full.get("mfe_full_pct")
        mae_full = stats_full.get("mae_full_pct")
        quick_win = signal in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND") and age_minutes <= float(args.quick_win_minutes)
        forced_exit = signal in ("TIME_EXIT", "TIME_EXIT_EARLY", "STOP_LOSS", "SELL")
        stale_detail = "stale_reversal" in exit_detail.lower()
        stuck = (
            forced_exit
            or stale_detail
            or age_minutes >= float(args.stuck_age_minutes)
            or (realized_pct is not None and realized_pct <= float(args.stuck_realized_pct))
        ) and not quick_win
        bad_reversal = (
            mae_full is not None
            and mae_full <= float(args.bad_reversal_mae_pct)
            and (realized_pct is None or realized_pct <= float(args.recovered_realized_pct))
        )
        recoverable_hanger = (
            not quick_win
            and not bad_reversal
            and mfe_full is not None
            and mfe_full >= float(args.recoverable_mfe_pct)
            and (realized_pct is None or realized_pct >= float(args.recoverable_min_realized_pct))
        )

        row: Dict[str, Any] = {
            "trade_id": int(getattr(t, "trade_id", 0) or 0),
            "ticker": str(t.ticker),
            "entry_ts_et": entry_et.isoformat(),
            "exit_ts_et": exit_et.isoformat(),
            "age_minutes": round(age_minutes, 2),
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "quantity": _safe_float(getattr(t, "quantity", None)),
            "net_pnl": _safe_float(getattr(t, "net_pnl", None)),
            "log_return": _safe_float(getattr(t, "log_return", None)),
            "realized_pct": realized_pct,
            "exit_signal_type": signal,
            "exit_detail_excerpt": exit_detail,
            "entry_decision": ctx.get("decision"),
            "entry_strategy": ctx.get("entry_strategy"),
            "entry_branch": ctx.get("technical_entry_branch"),
            "entry_session_phase": ctx.get("session_phase"),
            "entry_rsi_5m": _safe_float(ctx.get("rsi_5m")),
            "entry_momentum_2h_pct": _safe_float(ctx.get("momentum_2h_pct")),
            "entry_volatility_5m_pct": _safe_float(ctx.get("volatility_5m_pct")),
            "entry_atr_5m_pct": _safe_float(ctx.get("atr_5m_pct")),
            "entry_volume_vs_avg_pct": _safe_float(ctx.get("volume_vs_avg_pct")),
            "entry_prob_up": _safe_float(ctx.get("prob_up")),
            "entry_prob_down": _safe_float(ctx.get("prob_down")),
            "entry_estimated_upside_pct_day": _safe_float(ctx.get("estimated_upside_pct_day")),
            "entry_estimated_downside_pct_day": _safe_float(ctx.get("estimated_downside_pct_day")),
            "entry_take_profit_pct": _safe_float(ctx.get("take_profit_pct")),
            "entry_stop_loss_pct": _safe_float(ctx.get("stop_loss_pct")),
            "entry_bars_count": _safe_int(ctx.get("bars_count")),
            "entry_context_excerpt": _context_json_excerpt(ctx),
            "label_quick_win": int(bool(quick_win)),
            "label_stuck": int(bool(stuck)),
            "label_bad_reversal": int(bool(bad_reversal)),
            "label_recoverable_hanger": int(bool(recoverable_hanger)),
        }
        row.update(stats_30)
        row.update(stats_60)
        row.update(stats_full)
        yield row


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build GAME_5M stuck-risk training dataset")
    parser.add_argument("--out", default=str(project_root / "local" / "datasets" / "game5m_stuck_dataset.csv"))
    parser.add_argument("--exchange", default="US")
    parser.add_argument("--days-back", type=int, default=0, help="Only exits from last N days; 0 = all history")
    parser.add_argument("--quick-win-minutes", type=int, default=390)
    parser.add_argument("--stuck-age-minutes", type=int, default=1440)
    parser.add_argument("--stuck-realized-pct", type=float, default=-1.5)
    parser.add_argument("--bad-reversal-mae-pct", type=float, default=-3.0)
    parser.add_argument("--recovered-realized-pct", type=float, default=0.0)
    parser.add_argument("--recoverable-mfe-pct", type=float, default=1.5)
    parser.add_argument("--recoverable-min-realized-pct", type=float, default=-1.0)
    parser.add_argument("--limit", type=int, default=0, help="Debug: max rows to write")
    parser.add_argument("--dry-run", action="store_true", help="Print stats only, do not write CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    rows = list(_iter_dataset_rows(args))
    if args.limit and args.limit > 0:
        rows = rows[: int(args.limit)]

    logger.info("Dataset rows: %s", len(rows))
    for label in ("label_quick_win", "label_stuck", "label_bad_reversal", "label_recoverable_hanger"):
        logger.info("%s=1: %s", label, sum(int(r.get(label) or 0) for r in rows))

    if args.dry_run:
        return 0

    out = Path(args.out)
    _write_csv(out, rows)
    meta = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": len(rows),
        "args": vars(args),
        "labels": {
            "label_quick_win": "TAKE_PROFIT/TAKE_PROFIT_SUSPEND within quick_win_minutes",
            "label_stuck": "forced/slow/negative exit and not quick_win",
            "label_bad_reversal": "MAE below bad_reversal_mae_pct and no positive recovery",
            "label_recoverable_hanger": "not quick_win, not bad_reversal, enough MFE for reduced take",
        },
    }
    out.with_suffix(out.suffix + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

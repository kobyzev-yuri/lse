#!/usr/bin/env python3
"""
Build a supervised dataset for GAME_5M continuation / underprofit modeling.

Rows are GAME_5M trades closed by TAKE_PROFIT or TAKE_PROFIT_SUSPEND.
The labels answer a different question than stuck-risk:

  Did we take profit too early, leaving meaningful post-exit upside?

Output:
  python scripts/build_game5m_continuation_dataset.py \
    --out local/datasets/game5m_continuation_dataset.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime, timezone
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
TAKE_SIGNALS = {"TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"}


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


def _pct(base: float, price: Optional[float]) -> Optional[float]:
    if base <= 0 or price is None:
        return None
    return (float(price) / float(base) - 1.0) * 100.0


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


def _normalize_bars_et(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    d = pd.to_datetime(out["datetime"])
    if d.dt.tz is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        d = d.dt.tz_convert("America/New_York")
    out["datetime"] = d
    return out.sort_values("datetime").reset_index(drop=True)


def _slice_between(df: pd.DataFrame, start_et: pd.Timestamp, end_et: pd.Timestamp) -> pd.DataFrame:
    df = _normalize_bars_et(df)
    if df.empty:
        return df
    m = (df["datetime"] > start_et) & (df["datetime"] <= end_et)
    return df.loc[m].reset_index(drop=True)


def _window_stats(df: pd.DataFrame, base_price: float, prefix: str) -> Dict[str, Any]:
    if df is None or df.empty or base_price <= 0:
        return {
            f"{prefix}_bars": 0,
            f"{prefix}_return_pct": None,
            f"{prefix}_mfe_pct": None,
            f"{prefix}_mae_pct": None,
            f"{prefix}_range_pct": None,
            f"{prefix}_volume": None,
        }
    close_last = _safe_float(df.iloc[-1].get("Close"))
    high_max = _safe_float(pd.to_numeric(df["High"], errors="coerce").max())
    low_min = _safe_float(pd.to_numeric(df["Low"], errors="coerce").min())
    volume_sum = _safe_float(pd.to_numeric(df.get("Volume"), errors="coerce").fillna(0).sum()) if "Volume" in df else None
    mfe = _pct(base_price, high_max)
    mae = _pct(base_price, low_min)
    return {
        f"{prefix}_bars": int(len(df)),
        f"{prefix}_return_pct": _pct(base_price, close_last),
        f"{prefix}_mfe_pct": mfe,
        f"{prefix}_mae_pct": mae,
        f"{prefix}_range_pct": (mfe - mae) if mfe is not None and mae is not None else None,
        f"{prefix}_volume": volume_sum,
    }


def _context_excerpt(ctx: Dict[str, Any]) -> str:
    keys = (
        "decision",
        "technical_entry_branch",
        "entry_advice",
        "entry_strategy",
        "session_phase",
        "decision_rule_version",
    )
    slim = {k: ctx.get(k) for k in keys if ctx.get(k) is not None}
    return json.dumps(slim, ensure_ascii=False, sort_keys=True)


def _load_bars(engine: Any, ticker: str, entry_et: pd.Timestamp, exit_et: pd.Timestamp, lookahead_minutes: int, exchange: str) -> pd.DataFrame:
    start_utc = (entry_et.tz_convert("UTC") - pd.Timedelta(days=2)).floor("s")
    end_utc = (exit_et.tz_convert("UTC") + pd.Timedelta(minutes=max(30, int(lookahead_minutes)) + 60)).ceil("s")
    return load_bars_5m_for_replay(engine, ticker, exchange, start_utc, end_utc)


def _iter_dataset_rows(args: argparse.Namespace) -> Iterable[Dict[str, Any]]:
    engine = get_engine()
    trades = load_trade_history(engine, strategy_name=GAME_5M)
    closed = compute_closed_trade_pnls(trades)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(args.days_back)) if args.days_back else None

    for t in closed:
        if (getattr(t, "entry_strategy", "") or "").strip().upper() != GAME_5M:
            continue
        signal = (getattr(t, "signal_type", "") or "").strip().upper()
        if signal not in TAKE_SIGNALS and not args.include_non_take:
            continue

        entry_et = _ts_et(getattr(t, "entry_ts", None))
        exit_et = _ts_et(getattr(t, "ts", None))
        if entry_et is None or exit_et is None:
            continue
        if cutoff is not None and exit_et.tz_convert("UTC") < cutoff:
            continue

        entry_price = _safe_float(getattr(t, "entry_price", None))
        exit_price = _safe_float(getattr(t, "exit_price", None))
        if entry_price is None or entry_price <= 0 or exit_price is None or exit_price <= 0:
            continue

        try:
            df5 = _load_bars(engine, str(t.ticker), entry_et, exit_et, args.lookahead_minutes, args.exchange)
        except Exception as e:
            logger.warning("bars load failed trade_id=%s ticker=%s: %s", getattr(t, "trade_id", "?"), t.ticker, e)
            df5 = pd.DataFrame()

        exit_end = exit_et + pd.Timedelta(minutes=int(args.lookahead_minutes))
        post_exit = _slice_between(df5, exit_et, exit_end)
        pre_exit_30 = _slice_between(df5, exit_et - pd.Timedelta(minutes=30), exit_et)
        pre_exit_60 = _slice_between(df5, exit_et - pd.Timedelta(minutes=60), exit_et)
        full_trade = _slice_between(df5, entry_et, exit_et)

        entry_ctx = normalize_entry_context(getattr(t, "context_json", None))
        exit_ctx = normalize_entry_context(getattr(t, "exit_context_json", None))

        post_stats = _window_stats(post_exit, exit_price, "post_exit")
        pre30_stats = _window_stats(pre_exit_30, entry_price, "pre_exit_30m")
        pre60_stats = _window_stats(pre_exit_60, entry_price, "pre_exit_60m")
        trade_stats = _window_stats(full_trade, entry_price, "trade")

        age_minutes = max(0.0, (exit_et - entry_et).total_seconds() / 60.0)
        realized_pct = _pct(entry_price, exit_price)
        post_mfe = post_stats.get("post_exit_mfe_pct")
        post_mae = post_stats.get("post_exit_mae_pct")
        post_ret = post_stats.get("post_exit_return_pct")

        missed_upside = post_mfe is not None and post_mfe >= float(args.min_extra_upside_pct)
        take_was_enough = (
            not missed_upside
            and (
                post_ret is None
                or post_ret <= float(args.flat_post_exit_return_pct)
                or (post_mae is not None and post_mae <= float(args.pullback_after_take_pct))
            )
        )
        stretch_candidate = (
            missed_upside
            and age_minutes <= float(args.max_minutes_to_take_for_stretch)
            and (realized_pct is not None and realized_pct >= float(args.min_realized_take_pct))
        )

        take_pct = _safe_float(entry_ctx.get("take_profit_pct"))
        entry_vol = _safe_float(entry_ctx.get("volatility_5m_pct"))
        take_to_vol = take_pct / entry_vol if take_pct is not None and entry_vol and entry_vol > 0 else None

        row: Dict[str, Any] = {
            "trade_id": int(getattr(t, "trade_id", 0) or 0),
            "ticker": str(t.ticker),
            "entry_ts_et": entry_et.isoformat(),
            "exit_ts_et": exit_et.isoformat(),
            "minutes_to_exit": round(age_minutes, 2),
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "quantity": _safe_float(getattr(t, "quantity", None)),
            "net_pnl": _safe_float(getattr(t, "net_pnl", None)),
            "realized_pct": realized_pct,
            "exit_signal_type": signal,
            "entry_decision": entry_ctx.get("decision"),
            "entry_strategy": entry_ctx.get("entry_strategy"),
            "entry_branch": entry_ctx.get("technical_entry_branch"),
            "entry_session_phase": entry_ctx.get("session_phase"),
            "entry_rsi_5m": _safe_float(entry_ctx.get("rsi_5m")),
            "entry_momentum_2h_pct": _safe_float(entry_ctx.get("momentum_2h_pct")),
            "entry_volatility_5m_pct": entry_vol,
            "entry_atr_5m_pct": _safe_float(entry_ctx.get("atr_5m_pct")),
            "entry_volume_vs_avg_pct": _safe_float(entry_ctx.get("volume_vs_avg_pct")),
            "entry_prob_up": _safe_float(entry_ctx.get("prob_up")),
            "entry_prob_down": _safe_float(entry_ctx.get("prob_down")),
            "entry_estimated_upside_pct_day": _safe_float(entry_ctx.get("estimated_upside_pct_day")),
            "entry_take_profit_pct": take_pct,
            "entry_take_to_volatility": take_to_vol,
            "entry_bars_count": _safe_int(entry_ctx.get("bars_count")),
            "exit_momentum_2h_pct": _safe_float(exit_ctx.get("momentum_2h_pct")),
            "exit_rsi_5m": _safe_float(exit_ctx.get("rsi_5m")),
            "exit_volatility_5m_pct": _safe_float(exit_ctx.get("volatility_5m_pct")),
            "exit_session_high": _safe_float(exit_ctx.get("session_high")),
            "exit_bar_high": _safe_float(exit_ctx.get("bar_high")),
            "entry_context_excerpt": _context_excerpt(entry_ctx),
            "label_missed_upside": int(bool(missed_upside)),
            "label_take_was_enough": int(bool(take_was_enough)),
            "label_stretch_candidate": int(bool(stretch_candidate)),
        }
        row.update(trade_stats)
        row.update(pre30_stats)
        row.update(pre60_stats)
        row.update(post_stats)
        yield row


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build GAME_5M continuation / underprofit training dataset")
    parser.add_argument("--out", default=str(project_root / "local" / "datasets" / "game5m_continuation_dataset.csv"))
    parser.add_argument("--exchange", default="US")
    parser.add_argument("--days-back", type=int, default=0, help="Only exits from last N days; 0 = all history")
    parser.add_argument("--lookahead-minutes", type=int, default=120)
    parser.add_argument("--min-extra-upside-pct", type=float, default=1.0)
    parser.add_argument("--pullback-after-take-pct", type=float, default=-0.5)
    parser.add_argument("--flat-post-exit-return-pct", type=float, default=0.2)
    parser.add_argument("--max-minutes-to-take-for-stretch", type=int, default=390)
    parser.add_argument("--min-realized-take-pct", type=float, default=1.0)
    parser.add_argument("--include-non-take", action="store_true", help="Debug: include non-TAKE exits too")
    parser.add_argument("--limit", type=int, default=0, help="Debug: max rows to write")
    parser.add_argument("--dry-run", action="store_true", help="Print stats only, do not write CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    rows = list(_iter_dataset_rows(args))
    if args.limit and args.limit > 0:
        rows = rows[: int(args.limit)]

    logger.info("Dataset rows: %s", len(rows))
    for label in ("label_missed_upside", "label_take_was_enough", "label_stretch_candidate"):
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
            "label_missed_upside": "post-exit high exceeds exit price by min_extra_upside_pct",
            "label_take_was_enough": "no meaningful post-exit continuation or there was a pullback",
            "label_stretch_candidate": "quick profitable take that later had meaningful continuation",
        },
    }
    out.with_suffix(out.suffix + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Build unified hold-bar dataset for GAME_5M exit/hold ML bake-off.

Each row: (trade_id, bar_ts_et) inside an open GAME_5M long with y_hold_good
(recovery rule) and full context features (entry snapshot + exit tech + news/calendar).

  python scripts/build_game5m_hold_bar_dataset.py \\
    --source db --days 180 \\
    --out local/datasets/game5m_hold_bar_dataset.csv

See docs/GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from config_loader import get_config_value
from report_generator import compute_closed_trade_pnls, get_engine, load_trade_history
from services.game5m_hold_bar_dataset import HOLD_BAR_ML_SCHEMA_VERSION
from services.game5m_ml_context_features import (
    ENTRY_CONTEXT_NUMERIC_KEYS,
    build_entry_context_features,
    entry_snapshot_from_context,
    hold_exit_tech_from_features,
    hold_state_features,
)
from services.game5m_triple_barrier import forward_mfe_mae_pct_window, recovery_y_label
from services.game_5m import trade_ts_to_et
from services.game_5m_take_replay import load_bars_5m_for_replay
from services.recommend_5m import compute_5m_features
from scripts.build_game5m_entry_bar_dataset import _filter_kb_pool_as_of, _load_kb_pool_for_ticker

logger = logging.getLogger(__name__)

GAME_5M = "GAME_5M"


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _int_cfg(key: str, default: int) -> int:
    raw = (get_config_value(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_cfg(key: str, default: float) -> float:
    raw = (get_config_value(key) or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


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


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
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


def _json_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            p = json.loads(v)
            return p if isinstance(p, dict) else {}
        except Exception:
            return {}
    return {}


def _load_ticker_bars(ticker: str, *, days: int, exchange: str, engine: Any) -> pd.DataFrame:
    end_utc = pd.Timestamp.now(tz="UTC").ceil("s")
    start_utc = (end_utc - pd.Timedelta(days=int(days) + 5)).floor("s")
    return _normalize_df(
        load_bars_5m_for_replay(
            engine,
            ticker,
            exchange,
            start_utc,
            end_utc,
            yfinance_days=max(7, int(days)),
        )
    )


OUTPUT_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "trade_id",
    "ticker",
    "bar_ts_et",
    "entry_ts_et",
    "entry_price",
    "ref_close",
    "exit_signal",
    "y_hold_good",
    "h_minutes",
    "mfe_fwd_pct",
    "mae_fwd_pct",
    "label_eps_up_pct",
    "label_max_adverse_pct",
    "entry_decision",
    "sample_kind",
    "pnl_pct",
    "hold_minutes",
    "minutes_after_rth_open",
    "dow",
    "hour_et",
    "entry_rsi_5m",
    "entry_vol_5m_pct",
    "entry_momentum_2h_pct",
    "entry_kb_news_impact_enc",
    "entry_prob_up",
    "entry_prob_down",
    "entry_macro_risk_enc",
    "rsi_5m",
    "momentum_2h_pct",
    "volatility_5m_pct",
    "pullback_from_high_pct",
) + ENTRY_CONTEXT_NUMERIC_KEYS


def main() -> int:
    parser = argparse.ArgumentParser(description="Build GAME_5M hold-bar dataset (y_hold_good)")
    parser.add_argument("--out", type=str, default="", help="Output CSV path")
    parser.add_argument("--days", type=int, default=180, help="OHLC lookback days")
    parser.add_argument("--exchange", default="US")
    parser.add_argument("--source", choices=("db",), default="db")
    parser.add_argument("--horizon", type=int, default=None, help="Forward minutes (default config 120)")
    parser.add_argument("--stride", type=int, default=None, help="Bar stride per trade")
    parser.add_argument("--max-rows-per-trade", type=int, default=None)
    parser.add_argument("--no-enrich", action="store_true", help="Skip KB/calendar at hold bar")
    parser.add_argument("--kb-days", type=int, default=7)
    parser.add_argument("--summary-json", type=str, default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    h_min = int(args.horizon) if args.horizon is not None else 120
    raw_h = (get_config_value("GAME_5M_RECOVERY_ML_HORIZONS_MINUTES", "120") or "120").strip()
    if args.horizon is None and raw_h:
        try:
            h_min = int(raw_h.split(",")[0].strip())
        except ValueError:
            pass
    eps_up = _float_cfg("GAME_5M_RECOVERY_ML_EPS_UP_PCT", 0.5)
    max_adv = _float_cfg("GAME_5M_RECOVERY_ML_MAX_ADVERSE_PCT", -3.0)
    stride = args.stride if args.stride is not None else _int_cfg("GAME_5M_RECOVERY_ML_ROW_STRIDE", 2)
    stride = max(1, min(12, int(stride)))
    max_per_trade = args.max_rows_per_trade
    if max_per_trade is None:
        max_per_trade = _int_cfg("GAME_5M_RECOVERY_ML_MAX_ROWS_PER_TRADE", 40)
    max_per_trade = max(4, min(200, int(max_per_trade)))
    enrich = not bool(args.no_enrich)
    kb_days = max(1, int(args.kb_days))

    engine = get_engine()
    trades = load_trade_history(engine, strategy_name=GAME_5M)
    closed = [t for t in compute_closed_trade_pnls(trades) if str(t.exit_strategy or "").strip().upper() == GAME_5M]
    if not closed:
        logger.error("no closed GAME_5M trades")
        return 1

    ohlc_cache: dict[str, pd.DataFrame] = {}
    all_rows: list[dict[str, Any]] = []

    for trade in closed:
        ticker = str(trade.ticker or "").strip().upper()
        if not ticker:
            continue
        if ticker not in ohlc_cache:
            try:
                ohlc_cache[ticker] = _load_ticker_bars(ticker, days=int(args.days), exchange=str(args.exchange), engine=engine)
            except Exception as e:
                logger.warning("skip %s bars: %s", ticker, e)
                ohlc_cache[ticker] = pd.DataFrame()
        df = ohlc_cache[ticker]
        if df.empty:
            continue

        entry_ts = _ts_et(trade.entry_ts)
        exit_ts = _ts_et(trade.ts)
        entry_price = _safe_float(trade.entry_price)
        if entry_ts is None or exit_ts is None or entry_price is None or entry_price <= 0:
            continue
        if exit_ts <= entry_ts:
            continue

        entry_ctx = _json_dict(trade.context_json)
        entry_snap = entry_snapshot_from_context(entry_ctx)
        entry_decision = str(entry_ctx.get("decision") or "")[:64]
        exit_signal = str(trade.signal_type or _json_dict(trade.exit_context_json).get("exit_signal") or "")

        try:
            mask = (df["datetime"] >= entry_ts) & (df["datetime"] < exit_ts)
            sub = df.loc[mask].reset_index(drop=True)
        except Exception:
            continue
        if len(sub) < 2:
            continue

        n_trade_rows = 0
        kb_pool: list[dict[str, Any]] = []
        gaps_cache: dict[tuple[str, str], dict[str, float]] = {}
        index_gaps_cache: dict[str, dict[str, float]] = {}
        correlation_cache: dict = {}
        if enrich:
            kb_pool = _load_kb_pool_for_ticker(engine, ticker, kb_days=kb_days)
        for i in range(0, len(sub), stride):
            if n_trade_rows >= max_per_trade:
                break
            bar_time = pd.Timestamp(sub["datetime"].iloc[i])
            if bar_time.tzinfo is None:
                bar_time = bar_time.tz_localize("America/New_York", ambiguous=True)
            else:
                bar_time = bar_time.tz_convert("America/New_York")
            ref_close = _safe_float(sub["Close"].iloc[i])
            if ref_close is None or ref_close <= 0:
                continue

            end_t = bar_time + pd.Timedelta(minutes=int(h_min))
            mfe_pct, mae_pct = forward_mfe_mae_pct_window(
                df,
                ref_close=ref_close,
                start_ts=bar_time,
                end_ts=end_t,
            )
            y_hold = recovery_y_label(mfe_pct, mae_pct, eps_up_pct=eps_up, max_adverse_pct=max_adv)
            if y_hold is None:
                continue

            slice_df = df.loc[df["datetime"] <= bar_time].reset_index(drop=True)
            exit_features = compute_5m_features(slice_df, ticker) or {}
            state = hold_state_features(
                entry_price=entry_price,
                entry_ts_et=entry_ts,
                bar_ts_et=bar_time,
                ref_close=ref_close,
            )
            exit_tech = hold_exit_tech_from_features(exit_features)
            as_of_utc = bar_time.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
            kb_news = _filter_kb_pool_as_of(kb_pool, as_of_utc=as_of_utc, kb_days=kb_days) if enrich else []
            ctx_at_bar = (
                build_entry_context_features(
                    ticker=ticker,
                    bar_ts_et=bar_time.isoformat(),
                    features=exit_features,
                    entry_context=entry_ctx,
                    engine=engine if enrich else None,
                    kb_days=kb_days,
                    kb_news=kb_news,
                    gaps_cache=gaps_cache,
                    index_gaps_cache=index_gaps_cache,
                    correlation_cache=correlation_cache,
                )
                if enrich
                else {k: 0.0 for k in ENTRY_CONTEXT_NUMERIC_KEYS}
            )

            row: dict[str, Any] = {
                "schema_version": HOLD_BAR_ML_SCHEMA_VERSION,
                "trade_id": int(trade.trade_id),
                "ticker": ticker,
                "bar_ts_et": bar_time.isoformat(),
                "entry_ts_et": entry_ts.isoformat(),
                "entry_price": round(entry_price, 6),
                "ref_close": round(ref_close, 6),
                "exit_signal": exit_signal,
                "y_hold_good": int(y_hold),
                "h_minutes": int(h_min),
                "mfe_fwd_pct": round(float(mfe_pct), 4),
                "mae_fwd_pct": round(float(mae_pct), 4),
                "label_eps_up_pct": eps_up,
                "label_max_adverse_pct": max_adv,
                "entry_decision": entry_decision,
                "sample_kind": "hold_bar",
                **state,
                **entry_snap,
                **exit_tech,
                **ctx_at_bar,
            }
            all_rows.append(row)
            n_trade_rows += 1

        logger.info("trade %s %s: +%d hold rows", trade.trade_id, ticker, n_trade_rows)

    stats = {
        "n_rows": len(all_rows),
        "n_trades": len(closed),
        "horizon_minutes": h_min,
        "stride": stride,
        "enrich_context": enrich,
        "y_hold_good_rate": round(sum(int(r["y_hold_good"]) for r in all_rows) / len(all_rows), 4) if all_rows else 0,
    }
    logger.info("hold-bar stats: %s", json.dumps(stats, ensure_ascii=False))

    summary_path = (args.summary_json or "").strip()
    if summary_path:
        Path(summary_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    if args.dry_run:
        return 0

    out_arg = (args.out or "").strip()
    if not out_arg:
        logger.error("--out required unless --dry-run")
        return 1

    out_path = Path(out_arg).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(OUTPUT_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)
    logger.info("wrote %d rows → %s", len(all_rows), out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

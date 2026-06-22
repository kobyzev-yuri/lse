#!/usr/bin/env python3
"""
Build bar-level GAME_5M entry dataset with triple-barrier labels (phase 1.1–1.3).

Each row is (ticker, bar_ts) where technical_decision is BUY/STRONG_BUY, plus a
subsample of HOLD bars for negative class balance. Labels come from
services.game5m_triple_barrier (path-dependent upper/lower/time).

Does not touch prod CatBoost v1 (trade-based training).

  python scripts/build_game5m_entry_bar_dataset.py \\
    --out local/datasets/game5m_entry_bar_dataset.csv --days 90

See docs/GAME_5M_PREDICTOR_DATASET_PLAN.md
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sys
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from config_loader import get_config_value
from report_generator import get_engine
from services.game5m_entry_bar_dataset import (
    ENTRY_BAR_ML_SCHEMA_VERSION,
    ENTRY_CONTEXT_NUMERIC_KEYS,
    triple_barrier_config_from_env,
    triple_barrier_forward,
)
from services.game5m_ml_context_features import build_entry_context_features, merge_context_into_row
from services.game_5m_take_replay import load_bars_5m_for_replay
from services.recommend_5m import (
    BARS_2H,
    GAME_5M_RULE_VERSION,
    RSI_PERIOD_5M,
    US_SESSION_END,
    US_SESSION_START,
    compute_5m_features,
    compute_rsi_5m,
    decide_game5m_technical,
    fetch_5m_ohlc,
    get_decision_5m_rule_thresholds,
)
from services.ticker_groups import get_tickers_game_5m

logger = logging.getLogger(__name__)

BUY_DECISIONS = frozenset({"BUY", "STRONG_BUY"})

OUTPUT_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "ticker",
    "bar_ts_et",
    "technical_decision",
    "technical_entry_branch",
    "entry_strong_buy_downgraded",
    "price",
    "rsi_5m",
    "momentum_2h_pct",
    "momentum_rth_today_pct",
    "momentum_rth_today_bars",
    "volatility_5m_pct",
    "pullback_from_high_pct",
    "bars_count",
    "price_to_low5d_ratio",
    "low_5d",
    "high_5d",
    "tb_label",
    "y_entry_good",
    "tb_upper_pct",
    "tb_lower_pct",
    "tb_bars_forward",
    "tb_minutes_forward",
    "tb_mfe_pct",
    "tb_mae_pct",
    "sample_kind",
) + ENTRY_CONTEXT_NUMERIC_KEYS


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


def _float_cfg(key: str, default: float) -> float:
    raw = (get_config_value(key) or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int_cfg(key: str, default: int) -> int:
    raw = (get_config_value(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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


def _bar_is_rth(df: pd.DataFrame, idx: int) -> bool:
    if idx < 0 or idx >= len(df):
        return False
    ts = pd.Timestamp(df.iloc[idx]["datetime"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York", ambiguous=True)
    else:
        ts = ts.tz_convert("America/New_York")
    t_start = dt_time(*US_SESSION_START)
    t_end = dt_time(*US_SESSION_END)
    return t_start <= ts.time() <= t_end


def _keep_hold_row(ticker: str, bar_ts: str, ratio: float) -> bool:
    if ratio <= 0:
        return False
    if ratio >= 1.0:
        return True
    digest = hashlib.md5(f"{ticker}|{bar_ts}".encode()).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < ratio


def _rsi_prev_values(closes: pd.Series, sell_confirm_bars: int) -> list[float]:
    out: list[float] = []
    for back in range(1, sell_confirm_bars + 1):
        if len(closes) > back + RSI_PERIOD_5M:
            rv = compute_rsi_5m(closes.iloc[: -back], period=RSI_PERIOD_5M)
            if rv is not None:
                out.append(float(rv))
    return out


def _technical_at_bar(
    ticker: str,
    df: pd.DataFrame,
    idx: int,
    th: dict[str, Any],
    *,
    min_sess_bars: int,
    sell_confirm_bars: int,
    early_use_premarket_mom: bool,
) -> tuple[str, str | None, bool] | None:
    slice_df = df.iloc[: idx + 1]
    features = compute_5m_features(slice_df, ticker)
    if features is None:
        return None
    closes = slice_df["Close"].astype(float)
    rsi_prev = _rsi_prev_values(closes, sell_confirm_bars)
    decision_rule_params = {
        "rule_version": GAME_5M_RULE_VERSION,
        "source_fn": "scripts.build_game5m_entry_bar_dataset",
    }
    decision, _, branch, downgraded = decide_game5m_technical(
        ticker=ticker,
        features=features,
        closes=closes,
        th=th,
        rsi_prev_values=rsi_prev,
        decision_rule_params=decision_rule_params,
        min_session_bars=min_sess_bars,
        premarket_intraday_momentum_pct=None,
        early_use_premarket_mom=early_use_premarket_mom,
    )
    return decision, branch, downgraded


def _load_ticker_bars(
    ticker: str,
    *,
    days: int,
    source: str,
    exchange: str,
    engine: Any,
) -> pd.DataFrame:
    if source == "db":
        end_utc = pd.Timestamp.now(tz="UTC").ceil("s")
        start_utc = (end_utc - pd.Timedelta(days=int(days) + 3)).floor("s")
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
    raw = fetch_5m_ohlc(ticker, days=days)
    return _normalize_df(raw) if raw is not None else pd.DataFrame()


def _load_kb_pool_for_ticker(engine: Any, ticker: str, *, kb_days: int, extra_days: int = 30) -> list[dict[str, Any]]:
    """Prefetch KB rows for ticker; filter per bar in memory (builder speed)."""
    if engine is None:
        return []
    try:
        from datetime import datetime, timedelta

        from sqlalchemy import text

        end = datetime.utcnow()
        cutoff = end - timedelta(days=int(kb_days) + int(extra_days))
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT ts, ticker, source, content, sentiment_score, insight,
                           COALESCE(ingested_at, ts) AS eff_ts
                    FROM knowledge_base
                    WHERE (ticker = :ticker OR ticker IN ('MACRO', 'US_MACRO'))
                      AND COALESCE(ingested_at, ts) >= :cutoff
                      AND content IS NOT NULL
                      AND LENGTH(TRIM(content)) > 0
                    ORDER BY COALESCE(ingested_at, ts) DESC
                    """
                ),
                {"ticker": ticker, "cutoff": cutoff},
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            ts, kb_ticker, source, content, sentiment_score, insight, eff_ts = row
            out.append(
                {
                    "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "eff_ts": eff_ts,
                    "ticker": kb_ticker,
                    "source": source or "",
                    "content": (content or "")[:500],
                    "sentiment_score": float(sentiment_score) if sentiment_score is not None else None,
                    "insight": (insight or "")[:300] if insight else None,
                }
            )
        return out
    except Exception as e:
        logger.warning("KB pool load failed for %s: %s", ticker, e)
        return []


def _filter_kb_pool_as_of(pool: list[dict[str, Any]], *, as_of_utc: Any, kb_days: int) -> list[dict[str, Any]]:
    if not pool:
        return []
    from datetime import timedelta

    end = pd.Timestamp(as_of_utc)
    if end.tzinfo is not None:
        end = end.tz_convert("UTC").tz_localize(None)
    cutoff = end - pd.Timedelta(days=int(kb_days))
    kept: list[dict[str, Any]] = []
    for n in pool:
        eff = n.get("eff_ts") or n.get("ts")
        try:
            t = pd.Timestamp(eff)
            if t.tzinfo is not None:
                t = t.tz_convert("UTC").tz_localize(None)
        except Exception:
            continue
        if cutoff <= t <= end:
            kept.append(n)
    return kept[:50]


def _iter_rows_for_ticker(
    ticker: str,
    df: pd.DataFrame,
    *,
    tb_cfg: Any,
    th: dict[str, Any],
    min_sess_bars: int,
    sell_confirm_bars: int,
    early_use_premarket_mom: bool,
    neg_ratio: float,
    min_warmup: int,
    max_forward: int,
    enrich: bool,
    engine: Any,
    kb_days: int,
) -> Iterable[dict[str, Any]]:
    if df.empty:
        return
    upper_eff = tb_cfg.effective_upper_pct()
    lower_eff = tb_cfg.effective_lower_pct()
    last_idx = len(df) - max_forward - 1
    if last_idx < min_warmup:
        return

    kb_pool: list[dict[str, Any]] = []
    gaps_cache: dict[tuple[str, str], dict[str, float]] = {}
    if enrich and engine is not None:
        kb_pool = _load_kb_pool_for_ticker(engine, ticker, kb_days=kb_days)

    for idx in range(min_warmup, last_idx + 1):
        if not _bar_is_rth(df, idx):
            continue
        bar_ts = pd.Timestamp(df.iloc[idx]["datetime"])
        bar_ts_et = bar_ts.isoformat()
        as_of_utc = bar_ts.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
        kb_news = _filter_kb_pool_as_of(kb_pool, as_of_utc=as_of_utc, kb_days=kb_days) if enrich else []
        tech = _technical_at_bar(
            ticker,
            df,
            idx,
            th,
            min_sess_bars=min_sess_bars,
            sell_confirm_bars=sell_confirm_bars,
            early_use_premarket_mom=early_use_premarket_mom,
        )
        if tech is None:
            continue
        decision, branch, downgraded = tech
        if decision in BUY_DECISIONS:
            sample_kind = "buy_signal"
        elif decision == "HOLD":
            if not _keep_hold_row(ticker, bar_ts_et, neg_ratio):
                continue
            sample_kind = "hold_negative"
        else:
            continue

        tb = triple_barrier_forward(df, idx, config=tb_cfg)
        if tb.label == "insufficient_data":
            continue

        slice_df = df.iloc[: idx + 1]
        features = compute_5m_features(slice_df, ticker)
        if features is None:
            continue
        price = _safe_float(features.get("price"))
        low_5d = _safe_float(features.get("low_5d"))
        ratio = (price / low_5d) if price and low_5d and low_5d > 0 else None

        yield {
            "schema_version": ENTRY_BAR_ML_SCHEMA_VERSION,
            "ticker": ticker,
            "bar_ts_et": bar_ts_et,
            "technical_decision": decision,
            "technical_entry_branch": branch or "",
            "entry_strong_buy_downgraded": int(bool(downgraded)),
            "price": price,
            "rsi_5m": _safe_float(features.get("rsi_5m")),
            "momentum_2h_pct": _safe_float(features.get("momentum_2h_pct")),
            "momentum_rth_today_pct": _safe_float(features.get("momentum_rth_today_pct")),
            "momentum_rth_today_bars": int(features.get("momentum_rth_today_bars") or 0),
            "volatility_5m_pct": _safe_float(features.get("volatility_5m_pct")),
            "pullback_from_high_pct": _safe_float(features.get("pullback_from_high_pct")),
            "bars_count": int(features.get("bars_count") or len(slice_df)),
            "price_to_low5d_ratio": ratio,
            "low_5d": low_5d,
            "high_5d": _safe_float(features.get("high_5d")),
            "tb_label": tb.label,
            "y_entry_good": int(tb.y_entry_good),
            "tb_upper_pct": upper_eff,
            "tb_lower_pct": lower_eff,
            "tb_bars_forward": tb.bars_forward,
            "tb_minutes_forward": tb.minutes_forward,
            "tb_mfe_pct": tb.mfe_pct,
            "tb_mae_pct": tb.mae_pct,
            "sample_kind": sample_kind,
            **(
                build_entry_context_features(
                    ticker=ticker,
                    bar_ts_et=bar_ts_et,
                    features=features,
                    engine=engine if enrich else None,
                    kb_days=kb_days,
                    kb_news=kb_news if enrich else [],
                    gaps_cache=gaps_cache,
                )
                if enrich
                else {k: None for k in ENTRY_CONTEXT_NUMERIC_KEYS}
            ),
        }


def _summary_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n_rows": 0}
    y_pos = sum(int(r.get("y_entry_good") or 0) for r in rows)
    buy_rows = sum(1 for r in rows if r.get("sample_kind") == "buy_signal")
    hold_rows = sum(1 for r in rows if r.get("sample_kind") == "hold_negative")
    tb_counts: dict[str, int] = {}
    for r in rows:
        lbl = str(r.get("tb_label") or "")
        tb_counts[lbl] = tb_counts.get(lbl, 0) + 1
    tickers = sorted({str(r.get("ticker") or "") for r in rows if r.get("ticker")})
    return {
        "n_rows": len(rows),
        "n_tickers": len(tickers),
        "tickers": tickers,
        "n_buy_signal": buy_rows,
        "n_hold_negative": hold_rows,
        "y_entry_good_rate": round(y_pos / len(rows), 4),
        "tb_label_counts": tb_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build GAME_5M bar-level entry dataset with TB labels")
    parser.add_argument("--out", type=str, default="", help="Output CSV path (required unless --dry-run)")
    parser.add_argument("--days", type=int, default=90, help="Lookback days per ticker")
    parser.add_argument("--tickers", type=str, default="", help="Comma-separated; default GAME_5M_TICKERS / TICKERS_FAST")
    parser.add_argument("--source", choices=("yfinance", "db"), default="yfinance", help="OHLC source")
    parser.add_argument("--exchange", default="US", help="Exchange for DB bars")
    parser.add_argument("--neg-ratio", type=float, default=None, help="HOLD subsample ratio (default GAME_5M_ENTRY_BAR_NEG_RATIO)")
    parser.add_argument("--min-rows", type=int, default=None, help="Warn if fewer rows (GAME_5M_ENTRY_BAR_MIN_ROWS)")
    parser.add_argument("--summary-json", type=str, default="", help="Optional JSON stats path")
    parser.add_argument("--no-enrich", action="store_true", help="Technical features only (skip news/calendar)")
    parser.add_argument("--kb-days", type=int, default=7, help="KB lookback days for context enrich")
    parser.add_argument("--dry-run", action="store_true", help="Print stats only, no CSV")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else list(get_tickers_game_5m() or [])
    if not tickers:
        logger.error("No tickers: set --tickers or GAME_5M_TICKERS / TICKERS_FAST")
        return 1

    neg_ratio = args.neg_ratio
    if neg_ratio is None:
        neg_ratio = _float_cfg("GAME_5M_ENTRY_BAR_NEG_RATIO", 0.25)
    neg_ratio = max(0.0, min(1.0, float(neg_ratio)))

    min_rows_warn = args.min_rows
    if min_rows_warn is None:
        min_rows_warn = _int_cfg("GAME_5M_ENTRY_BAR_MIN_ROWS", 5000)

    th = get_decision_5m_rule_thresholds()
    min_sess_bars = int(th["momentum_min_session_bars"])
    sell_confirm_bars = int(th["sell_confirm_bars"])
    early_use_premarket_mom = (get_config_value("GAME_5M_EARLY_USE_PREMARKET_MOMENTUM", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    tb_cfg = triple_barrier_config_from_env()
    min_warmup = max(RSI_PERIOD_5M + sell_confirm_bars + 2, BARS_2H + 5, min_sess_bars + 2)
    max_forward = max(int(tb_cfg.max_bars), 1)

    enrich = not bool(args.no_enrich)
    kb_days = max(1, int(args.kb_days))

    engine = get_engine() if args.source == "db" or enrich else None
    all_rows: list[dict[str, Any]] = []

    for ticker in tickers:
        try:
            df = _load_ticker_bars(
                ticker,
                days=int(args.days),
                source=args.source,
                exchange=str(args.exchange),
                engine=engine,
            )
        except Exception as e:
            logger.warning("skip %s: load bars failed: %s", ticker, e)
            continue
        if df.empty:
            logger.warning("skip %s: no bars", ticker)
            continue
        n_before = len(all_rows)
        for row in _iter_rows_for_ticker(
            ticker,
            df,
            tb_cfg=tb_cfg,
            th=th,
            min_sess_bars=min_sess_bars,
            sell_confirm_bars=sell_confirm_bars,
            early_use_premarket_mom=early_use_premarket_mom,
            neg_ratio=neg_ratio,
            min_warmup=min_warmup,
            max_forward=max_forward,
            enrich=enrich,
            engine=engine,
            kb_days=kb_days,
        ):
            all_rows.append(row)
        logger.info("%s: +%d rows (bars=%d)", ticker, len(all_rows) - n_before, len(df))

    stats = _summary_stats(all_rows)
    stats["days"] = int(args.days)
    stats["enrich_context"] = enrich
    stats["kb_days"] = kb_days
    stats["neg_ratio"] = neg_ratio
    stats["tb_config"] = {
        "upper_pct": tb_cfg.upper_pct,
        "lower_pct": tb_cfg.lower_pct,
        "max_bars": tb_cfg.max_bars,
        "max_minutes": tb_cfg.max_minutes,
        "cost_bps": tb_cfg.cost_bps,
    }
    logger.info("dataset stats: %s", json.dumps(stats, ensure_ascii=False))

    if stats["n_rows"] < min_rows_warn:
        logger.warning(
            "row count %d < GAME_5M_ENTRY_BAR_MIN_ROWS=%d (extend --days or ticker universe)",
            stats["n_rows"],
            min_rows_warn,
        )

    summary_path = (args.summary_json or "").strip()
    if summary_path:
        outp = Path(summary_path).expanduser()
        outp.parent.mkdir(parents=True, exist_ok=True)
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    if args.dry_run:
        return 0

    out_arg = (args.out or "").strip()
    if not out_arg:
        logger.error("--out is required unless --dry-run")
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

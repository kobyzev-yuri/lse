#!/usr/bin/env python3
"""
Build chart-window NPZ for GAME_5M entry CNN/LSTM research (phase 0.2).

Reads bar-level CSV (triple-barrier y) and attaches OHLCV windows from DB or yfinance.
Local workflow with prod DB via SSH tunnel:

  ssh -L 5433:127.0.0.1:5432 ai8049520@104.154.205.58
  export DATABASE_URL=postgresql://postgres:PASS@127.0.0.1:5433/lse_trading

  # 1) Bar CSV (same as prod bar v2)
  python scripts/build_game5m_entry_bar_dataset.py \\
    --source db --days 90 \\
    --out local/datasets/game5m_entry_bar_dataset.csv \\
    --summary-json local/datasets/game5m_entry_bar_stats.json

  # 2) Chart tensors
  python scripts/build_game5m_chart_entry_dataset.py \\
    --bar-csv local/datasets/game5m_entry_bar_dataset.csv \\
    --source db --days 90 \\
    --out local/datasets/game5m_chart_entry_v1.npz \\
    --summary-json local/datasets/game5m_chart_entry_v1_stats.json

  # 3) LSTM baseline (local GPU)
  python scripts/train_game5m_chart_entry_lstm.py \\
    --npz local/datasets/game5m_chart_entry_v1.npz

See docs/GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine
from services.game5m_chart_entry_dataset import (
    CHART_ENTRY_ML_SCHEMA_VERSION,
    assign_time_splits,
    chart_dataset_summary,
    chart_feature_names,
    find_decision_bar_index,
    make_sample_id,
    window_tensor_from_df,
    window_tensor_with_context,
)
from services.game_5m_take_replay import load_bars_5m_for_replay

logger = logging.getLogger(__name__)


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
    from services.recommend_5m import fetch_5m_ohlc

    raw = fetch_5m_ohlc(ticker, days=days)
    return _normalize_df(raw) if raw is not None else pd.DataFrame()


def _read_bar_csv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            if str(raw.get("tb_label") or "") == "insufficient_data":
                continue
            ticker = (raw.get("ticker") or "").strip()
            bar_ts = (raw.get("bar_ts_et") or "").strip()
            if not ticker or not bar_ts:
                continue
            rows.append(raw)
    return rows


def _run_bar_builder(out_csv: Path, stats_json: Path, *, days: int, source: str, exchange: str) -> int:
    cmd = [
        sys.executable,
        str(project_root / "scripts/build_game5m_entry_bar_dataset.py"),
        "--days",
        str(days),
        "--source",
        source,
        "--exchange",
        exchange,
        "--out",
        str(out_csv),
        "--summary-json",
        str(stats_json),
    ]
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(project_root))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build GAME_5M chart entry NPZ (OHLCV windows + TB labels)")
    ap.add_argument("--bar-csv", type=str, default="", help="Input bar dataset CSV")
    ap.add_argument(
        "--build-bar-csv",
        action="store_true",
        help="Run build_game5m_entry_bar_dataset.py first (uses --days/--source)",
    )
    ap.add_argument("--out", type=str, default="", help="Output .npz path")
    ap.add_argument("--summary-json", type=str, default="", help="Stats JSON path")
    ap.add_argument("--days", type=int, default=90, help="OHLC lookback per ticker")
    ap.add_argument("--source", choices=("yfinance", "db"), default="db", help="OHLC source")
    ap.add_argument("--exchange", default="US", help="DB exchange code")
    ap.add_argument("--window-bars", type=int, default=48, help="Bars per window (inclusive at decision)")
    ap.add_argument("--valid-ratio", type=float, default=0.2, help="Time-ordered valid fraction")
    ap.add_argument("--no-context", action="store_true", help="OHLCV only (no broadcast news/calendar channels)")
    ap.add_argument("--dry-run", action="store_true", help="Stats only, no NPZ write")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    bar_csv = Path(args.bar_csv).expanduser() if args.bar_csv else project_root / "local/datasets/game5m_entry_bar_dataset.csv"
    stats_bar = bar_csv.with_name(bar_csv.stem + "_stats.json")

    if args.build_bar_csv:
        bar_csv.parent.mkdir(parents=True, exist_ok=True)
        rc = _run_bar_builder(bar_csv, stats_bar, days=int(args.days), source=args.source, exchange=str(args.exchange))
        if rc != 0:
            logger.error("bar CSV build failed rc=%s", rc)
            return rc

    if not bar_csv.is_file():
        logger.error("bar CSV not found: %s (use --build-bar-csv or --bar-csv)", bar_csv)
        return 1

    csv_rows = _read_bar_csv(bar_csv)
    if not csv_rows:
        logger.error("no rows in bar CSV")
        return 1

    by_ticker: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in csv_rows:
        by_ticker[row["ticker"]].append(row)

    engine = get_engine() if args.source == "db" else None
    window_bars = max(4, int(args.window_bars))

    tensors: list[np.ndarray] = []
    y_list: list[int] = []
    sample_ids: list[str] = []
    tickers: list[str] = []
    bar_ts_list: list[str] = []
    tb_labels: list[str] = []
    n_skipped = 0

    include_context = not bool(args.no_context)
    feat_names = chart_feature_names(include_context=include_context)

    for ticker in sorted(by_ticker.keys()):
        try:
            df = _load_ticker_bars(ticker, days=int(args.days), source=args.source, exchange=str(args.exchange), engine=engine)
        except Exception as e:
            logger.warning("skip %s: load bars: %s", ticker, e)
            n_skipped += len(by_ticker[ticker])
            continue
        if df.empty:
            logger.warning("skip %s: no bars", ticker)
            n_skipped += len(by_ticker[ticker])
            continue

        got = 0
        for row in by_ticker[ticker]:
            idx = find_decision_bar_index(df, row["bar_ts_et"])
            if idx is None:
                n_skipped += 1
                continue
            win = window_tensor_from_df(df, idx, window_bars=window_bars)
            if win is None:
                n_skipped += 1
                continue
            win = window_tensor_with_context(win, row, include_context=include_context)
            try:
                y = int(float(row.get("y_entry_good") or 0))
            except (TypeError, ValueError):
                n_skipped += 1
                continue
            tensors.append(win)
            y_list.append(1 if y else 0)
            sample_ids.append(make_sample_id(ticker, row["bar_ts_et"]))
            tickers.append(ticker)
            bar_ts_list.append(row["bar_ts_et"])
            tb_labels.append(str(row.get("tb_label") or ""))
            got += 1
        logger.info("%s: chart rows=%d / csv=%d (bars=%d)", ticker, got, len(by_ticker[ticker]), len(df))

    n_rows = len(tensors)
    if n_rows == 0:
        logger.error("no chart rows built (skipped=%d)", n_skipped)
        return 1

    splits = assign_time_splits(bar_ts_list, valid_ratio=float(args.valid_ratio))
    n_train = sum(1 for s in splits if s == "train")
    n_valid = sum(1 for s in splits if s == "valid")
    y_pos = sum(y_list)

    X = np.stack(tensors, axis=0).astype(np.float32)
    y = np.asarray(y_list, dtype=np.int8)
    split_arr = np.asarray(splits, dtype="U5")

    stats = chart_dataset_summary(
        n_rows=n_rows,
        n_skipped=n_skipped,
        y_pos=y_pos,
        n_train=n_train,
        n_valid=n_valid,
        window_bars=window_bars,
        bar_csv=str(bar_csv),
        source=str(args.source),
    )
    stats["include_context"] = include_context
    stats["n_features"] = len(feat_names)
    stats["feature_names"] = list(feat_names)
    stats["tb_label_counts"] = {
        k: tb_labels.count(k) for k in sorted(set(tb_labels))
    }
    stats["tickers"] = sorted(set(tickers))
    logger.info("chart stats: %s", json.dumps(stats, ensure_ascii=False))

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
        logger.error("--out required unless --dry-run")
        return 1

    out_path = Path(out_arg).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        schema_version=CHART_ENTRY_ML_SCHEMA_VERSION,
        X=X,
        y=y,
        split=split_arr,
        sample_id=np.asarray(sample_ids, dtype=object),
        ticker=np.asarray(tickers, dtype=object),
        bar_ts_et=np.asarray(bar_ts_list, dtype=object),
        tb_label=np.asarray(tb_labels, dtype=object),
        feature_names=np.asarray(list(feat_names), dtype=object),
        context_feature_names=np.asarray(
            list(chart_feature_names(include_context=True)) if include_context else [],
            dtype=object,
        ),
        include_context=np.bool_(include_context),
        window_bars=np.int32(window_bars),
    )
    logger.info("wrote %d rows shape=%s → %s", n_rows, X.shape, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

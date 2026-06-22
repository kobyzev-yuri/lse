#!/usr/bin/env python3
"""Add ENTRY_CONTEXT_NUMERIC_KEYS columns to existing bar CSV (no TB recompute)."""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine
from services.game5m_ml_context_features import ENTRY_CONTEXT_NUMERIC_KEYS, build_entry_context_features
from scripts.build_game5m_entry_bar_dataset import _filter_kb_pool_as_of, _load_kb_pool_for_ticker

logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich bar CSV with news/calendar context columns")
    ap.add_argument("--in-csv", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--kb-days", type=int, default=7)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    in_path = Path(args.in_csv).expanduser()
    out_path = Path(args.out_csv).expanduser()
    if not in_path.is_file():
        logger.error("input not found: %s", in_path)
        return 1

    engine = get_engine()
    kb_days = max(1, int(args.kb_days))
    kb_pools: dict[str, list] = {}
    gaps_cache: dict = {}

    rows_out: list[dict] = []
    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_fields = list(reader.fieldnames or [])
        out_fields = base_fields + [k for k in ENTRY_CONTEXT_NUMERIC_KEYS if k not in base_fields]
        for i, row in enumerate(reader):
            ticker = (row.get("ticker") or "").strip().upper()
            bar_ts = (row.get("bar_ts_et") or "").strip()
            if ticker not in kb_pools:
                kb_pools[ticker] = _load_kb_pool_for_ticker(engine, ticker, kb_days=kb_days)
            import pandas as pd

            as_of = pd.Timestamp(bar_ts).tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
            kb_news = _filter_kb_pool_as_of(kb_pools[ticker], as_of_utc=as_of, kb_days=kb_days)
            ctx = build_entry_context_features(
                ticker=ticker,
                bar_ts_et=bar_ts,
                features=row,
                engine=engine,
                kb_days=kb_days,
                kb_news=kb_news,
                gaps_cache=gaps_cache,
            )
            row.update(ctx)
            rows_out.append(row)
            if (i + 1) % 2000 == 0:
                logger.info("enriched %d rows", i + 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for row in rows_out:
            w.writerow(row)
    logger.info("wrote %d rows → %s", len(rows_out), out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

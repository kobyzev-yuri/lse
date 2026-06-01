#!/usr/bin/env python3
"""
Label open-path scenarios from game5m_gap_forecast_daily + RTH close.

Rule labels after close; features snapshot from premarket_daily_features + gap forecast pre-open fields.

Examples:
  python scripts/label_open_path_scenarios.py --ensure-table --dry-run
  python scripts/label_open_path_scenarios.py --ensure-table --since 2026-01-01
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DDL_PATH = project_root / "db/knowledge_pg/sql/028_game5m_open_path_labels.sql"

UPSERT_SQL = """
INSERT INTO game5m_open_path_labels (
  trade_date, symbol, exchange,
  scenario_label, label_source, rule_version,
  open_gap_pct, rth_open_price, rth_close_price,
  close_open_log_ret, fade_from_gap_pct,
  features_before, feature_builder_version,
  label_status, updated_at
)
VALUES (
  :trade_date, :symbol, :exchange,
  :scenario_label, :label_source, :rule_version,
  :open_gap_pct, :rth_open_price, :rth_close_price,
  :close_open_log_ret, :fade_from_gap_pct,
  CAST(:features_before AS jsonb), :feature_builder_version,
  :label_status, NOW()
)
ON CONFLICT (exchange, symbol, trade_date) DO UPDATE SET
  scenario_label = EXCLUDED.scenario_label,
  label_source = EXCLUDED.label_source,
  rule_version = EXCLUDED.rule_version,
  open_gap_pct = EXCLUDED.open_gap_pct,
  rth_open_price = EXCLUDED.rth_open_price,
  rth_close_price = EXCLUDED.rth_close_price,
  close_open_log_ret = EXCLUDED.close_open_log_ret,
  fade_from_gap_pct = EXCLUDED.fade_from_gap_pct,
  features_before = EXCLUDED.features_before,
  feature_builder_version = EXCLUDED.feature_builder_version,
  label_status = EXCLUDED.label_status,
  updated_at = NOW()
"""


def ensure_open_path_labels_table(engine) -> None:
    from sqlalchemy import text

    sql = DDL_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))


def main() -> int:
    ap = argparse.ArgumentParser(description="Label GAME_5M open-path scenarios (rule after close)")
    ap.add_argument("--ensure-table", action="store_true")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Relabel even if row exists")
    args = ap.parse_args()

    from sqlalchemy import text

    from report_generator import get_engine
    from services.open_path_classifier_dataset import (
        FEATURE_BUILDER_VERSION,
        build_features_snapshot,
        collect_open_path_classifier_coverage,
        fetch_rth_close_price,
    )
    from services.open_path_labels import LABEL_SOURCE, RULE_VERSION, classify_open_path_scenario

    engine = get_engine()
    if args.ensure_table:
        ensure_open_path_labels_table(engine)
        logger.info("OK: game5m_open_path_labels")

    q = text(
        """
        SELECT
          g.trade_date,
          g.symbol,
          g.open_gap_pct,
          g.rth_open_price,
          g.premarket_gap_pct,
          g.pred_sector_gap_pct,
          g.pred_ticker_gap_pct,
          g.macro_equity_gap_bias,
          g.macro_risk_level
        FROM game5m_gap_forecast_daily g
        LEFT JOIN game5m_open_path_labels l
          ON l.trade_date = g.trade_date
         AND l.symbol = g.symbol
         AND l.exchange = 'US'
        WHERE g.open_gap_pct IS NOT NULL
          AND g.rth_open_price IS NOT NULL
          AND g.trade_date >= CAST(:since AS date)
          AND (:force OR l.trade_date IS NULL)
        ORDER BY g.trade_date DESC, g.symbol
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            q,
            {"since": args.since.strip()[:10], "lim": max(1, int(args.limit)), "force": bool(args.force)},
        ).mappings().all()

    n_ok = 0
    n_missing_close = 0
    class_counts: dict[str, int] = {}

    for r in rows:
        sym = str(r["symbol"]).strip().upper()
        td = r["trade_date"]
        open_px = float(r["rth_open_price"])
        gap_pct = float(r["open_gap_pct"])
        close_px, close_src = fetch_rth_close_price(engine, symbol=sym, trade_date=td)
        gf_row = dict(r)

        with engine.connect() as conn:
            pm = conn.execute(
                text(
                    """
                    SELECT *
                    FROM premarket_daily_features
                    WHERE symbol = :sym AND trade_date = :td AND exchange = 'US'
                    ORDER BY snapshot_ts_utc DESC
                    LIMIT 1
                    """
                ),
                {"sym": sym, "td": td},
            ).mappings().first()
        pm_row = dict(pm) if pm else None
        features = build_features_snapshot(pm_row=pm_row, gf_row=gf_row)

        if close_px is None:
            n_missing_close += 1
            payload = {
                "trade_date": td,
                "symbol": sym,
                "exchange": "US",
                "scenario_label": "open_flat_chop",
                "label_source": LABEL_SOURCE,
                "rule_version": RULE_VERSION,
                "open_gap_pct": gap_pct,
                "rth_open_price": open_px,
                "rth_close_price": None,
                "close_open_log_ret": None,
                "fade_from_gap_pct": None,
                "features_before": json.dumps(features, ensure_ascii=False),
                "feature_builder_version": FEATURE_BUILDER_VERSION,
                "label_status": "missing_close",
            }
            if not args.dry_run:
                with engine.begin() as conn:
                    conn.execute(text(UPSERT_SQL), payload)
            continue

        scenario, meta = classify_open_path_scenario(
            open_gap_pct=gap_pct,
            rth_open=open_px,
            rth_close=float(close_px),
        )
        class_counts[scenario] = class_counts.get(scenario, 0) + 1
        payload = {
            "trade_date": td,
            "symbol": sym,
            "exchange": "US",
            "scenario_label": scenario,
            "label_source": LABEL_SOURCE,
            "rule_version": RULE_VERSION,
            "open_gap_pct": gap_pct,
            "rth_open_price": open_px,
            "rth_close_price": float(close_px),
            "close_open_log_ret": meta["close_open_log_ret"],
            "fade_from_gap_pct": meta["fade_from_gap_pct"],
            "features_before": json.dumps(features, ensure_ascii=False),
            "feature_builder_version": FEATURE_BUILDER_VERSION,
            "label_status": "ok",
        }
        if args.dry_run:
            logger.info(
                "dry-run %s %s gap=%+.2f%% scenario=%s close_src=%s",
                td,
                sym,
                gap_pct,
                scenario,
                close_src,
            )
        else:
            with engine.begin() as conn:
                conn.execute(text(UPSERT_SQL), payload)
        n_ok += 1

    coverage = collect_open_path_classifier_coverage(engine, since=args.since)
    summary = {
        "mode": "dry_run" if args.dry_run else "ok",
        "candidates": len(rows),
        "labeled_ok": n_ok,
        "missing_close": n_missing_close,
        "class_counts": class_counts,
        "coverage": coverage,
    }
    logger.info("Open-path labeling: %s", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

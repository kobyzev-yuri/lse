#!/usr/bin/env python3
"""
Лог премаркет-прогноза гэпа и факта на open (GAME_5m).

  python scripts/ingest_game5m_gap_forecast.py --ensure-table
  python scripts/ingest_game5m_gap_forecast.py --phase premarket
  python scripts/ingest_game5m_gap_forecast.py --phase open
  python scripts/ingest_game5m_gap_forecast.py --phase all

Cron (ET): premarket вместе с premarket_cron; open — 9:35–10:00 ET, 1–2 раза.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest game5m gap forecast daily log")
    parser.add_argument("--ensure-table", action="store_true", help="Применить DDL 026")
    parser.add_argument(
        "--phase",
        choices=("premarket", "open", "all"),
        default="all",
        help="premarket=snapshot; open=fill RTH open; all=both",
    )
    parser.add_argument("--force", action="store_true", help="Перезаписать open при --phase open")
    parser.add_argument(
        "--trade-date",
        type=str,
        default=None,
        help="Календарный день ET (YYYY-MM-DD); по умолчанию — сегодня ET",
    )
    args = parser.parse_args()

    trade_date: date | None = None
    if args.trade_date:
        trade_date = date.fromisoformat(args.trade_date.strip())

    from services.game5m_gap_forecast import (
        ensure_gap_forecast_table,
        record_open_gaps_all,
        record_premarket_gap_snapshots,
    )

    if args.ensure_table:
        ensure_gap_forecast_table()
        print("OK: game5m_gap_forecast_daily")

    out = {}
    if args.phase in ("premarket", "all"):
        out["premarket"] = record_premarket_gap_snapshots(force=args.force, trade_date=trade_date)
    if args.phase in ("open", "all"):
        out["open"] = record_open_gaps_all(force=args.force, trade_date=trade_date)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

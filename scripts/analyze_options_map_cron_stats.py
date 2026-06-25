#!/usr/bin/env python3
"""
Статистика PCR volume из cron options_chain_oi_snapshot для калибровки порогов Money Map.

Считает квантили PCR vol (±strike_window_pct как у карты) per (ticker, expiration_date)
и предлагает bullish_max / bearish_min (p25/p75) при достаточной истории.

Usage
-----
  python3 scripts/analyze_options_map_cron_stats.py
  python3 scripts/analyze_options_map_cron_stats.py --days 90 --ticker MU --json-out local/logs/ml_data_quality/last_options_map_cron_stats.json

Prod:
  docker exec lse-bot python scripts/analyze_options_map_cron_stats.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.options_map_cron_stats import (  # noqa: E402
    build_options_map_cron_stats_report,
    default_report_path,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Options Money Map PCR stats from cron OI snapshots.")
    ap.add_argument("--days", type=int, default=90, help="Окно snapshot_date (1–365)")
    ap.add_argument("--ticker", action="append", default=[], help="Фильтр тикеров (repeatable)")
    ap.add_argument("--strike-window-pct", type=float, default=0.20, help="Окно страйков как у map")
    ap.add_argument("--min-samples", type=int, default=10, help="Мин. снимков для p25/p75 порогов")
    ap.add_argument("--daily-limit", type=int, default=45, help="Сколько daily_series в JSON на серию")
    ap.add_argument("--json-out", default="", help="Путь к JSON-артефакту")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.ticker if t.strip()] or None
    report = build_options_map_cron_stats_report(
        days=max(1, min(365, int(args.days))),
        tickers=tickers,
        strike_window_pct=float(args.strike_window_pct),
        min_samples=max(3, int(args.min_samples)),
        daily_series_limit=max(0, int(args.daily_limit)),
    )

    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)

    out_path = Path(args.json_out) if args.json_out else default_report_path(project_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"\nWrote: {out_path}", file=sys.stderr)

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    print(
        f"series={summary.get('series_total', 0)} "
        f"ready={summary.get('series_ready_for_quantiles', 0)}",
        file=sys.stderr,
    )
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

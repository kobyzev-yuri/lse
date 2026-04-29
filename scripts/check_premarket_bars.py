#!/usr/bin/env python3
"""
Check whether stored intraday bars contain premarket data.

Run on VM:
  docker compose exec lse python scripts/check_premarket_bars.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url


INTRADAY_TABLES = ("market_bars_5m", "market_bars_30m", "market_bars_1h")
DAILY_TABLES = (
    ("market_bars_daily", "symbol", "trade_date"),
    ("quotes", "ticker", "date"),
)

PHASE_CASE_SQL = """
CASE
  WHEN (bar_start_utc AT TIME ZONE 'America/New_York')::time < time '04:00' THEN 'overnight'
  WHEN (bar_start_utc AT TIME ZONE 'America/New_York')::time < time '09:30' THEN 'premarket'
  WHEN (bar_start_utc AT TIME ZONE 'America/New_York')::time < time '16:00' THEN 'regular'
  WHEN (bar_start_utc AT TIME ZONE 'America/New_York')::time < time '20:00' THEN 'afterhours'
  ELSE 'overnight'
END
"""


def _existing_tables(conn) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            """
        )
    )
    return {str(r[0]) for r in rows}


def _print_daily_summary(conn, existing: set[str]) -> None:
    print("DAILY TABLES")
    for table, symbol_col, ts_col in DAILY_TABLES:
        if table not in existing:
            print(f"{table}: MISSING")
            continue
        row = conn.execute(
            text(
                f"""
                SELECT COUNT(*) AS rows,
                       COUNT(DISTINCT {symbol_col}) AS symbols,
                       MIN({ts_col}) AS min_ts,
                       MAX({ts_col}) AS max_ts
                FROM {table}
                """
            )
        ).mappings().one()
        print(
            f"{table}: rows={row['rows']} symbols={row['symbols']} "
            f"min={row['min_ts']} max={row['max_ts']}"
        )


def _print_intraday_summary(conn, existing: set[str]) -> None:
    print("\nINTRADAY TABLES BY ET PHASE")
    for table in INTRADAY_TABLES:
        if table not in existing:
            print(f"\n{table}: MISSING")
            continue
        total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        print(f"\n{table}: total_rows={total}")
        if total == 0:
            continue
        rows = conn.execute(
            text(
                f"""
                SELECT {PHASE_CASE_SQL} AS phase,
                       COUNT(*) AS rows,
                       COUNT(DISTINCT symbol) AS symbols,
                       MIN(bar_start_utc AT TIME ZONE 'America/New_York') AS min_et,
                       MAX(bar_start_utc AT TIME ZONE 'America/New_York') AS max_et
                FROM {table}
                GROUP BY 1
                ORDER BY 1
                """
            )
        ).mappings()
        for r in rows:
            print(
                f"  {r['phase']}: rows={r['rows']} symbols={r['symbols']} "
                f"min_et={r['min_et']} max_et={r['max_et']}"
            )


def _print_recent_premarket_sample(conn, existing: set[str]) -> None:
    if "market_bars_5m" not in existing:
        return
    print("\nRECENT 5M PREMARKET SAMPLE")
    rows = conn.execute(
        text(
            """
            SELECT symbol,
                   bar_start_utc AT TIME ZONE 'America/New_York' AS et,
                   open,
                   close,
                   volume
            FROM market_bars_5m
            WHERE (bar_start_utc AT TIME ZONE 'America/New_York')::time >= time '04:00'
              AND (bar_start_utc AT TIME ZONE 'America/New_York')::time < time '09:30'
            ORDER BY bar_start_utc DESC
            LIMIT 20
            """
        )
    ).mappings()
    found = False
    for r in rows:
        found = True
        print(
            f"  {r['symbol']} {r['et']} "
            f"open={r['open']} close={r['close']} volume={r['volume']}"
        )
    if not found:
        print("  no 5m premarket bars found")


def main() -> None:
    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        existing = _existing_tables(conn)
        _print_daily_summary(conn, existing)
        _print_intraday_summary(conn, existing)
        _print_recent_premarket_sample(conn, existing)


if __name__ == "__main__":
    main()

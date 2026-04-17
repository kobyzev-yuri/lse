#!/usr/bin/env python3
"""
Первичная загрузка и пополнение 5m/30m баров в Postgres (market_bars_5m, market_bars_30m).

  # создать таблицы
  python scripts/migrate_market_bars_intraday.py

  # загрузить последние 7 дней по тикерам игры 5m (GAME_5M_TICKERS / TICKERS_FAST)
  python scripts/ingest_market_bars_intraday.py

  # явный список и 14 дней (30m до MAX_DAYS_30M, 5m всё равно max 7)
  python scripts/ingest_market_bars_intraday.py --days 14 --tickers SNDK,MU

Cron: см. setup_cron.sh / setup_cron_docker.sh — по умолчанию раз в сутки 23:25 (flock). Реже: заменить расписание на, например, `25 23 * * 0` (раз в неделю, воскресенье).

На VM:
  docker compose exec lse python scripts/migrate_market_bars_intraday.py
  docker compose exec lse python scripts/ingest_market_bars_intraday.py
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_database_url
from services.recommend_5m import MAX_DAYS_5M, MAX_DAYS_30M, fetch_30m_ohlc, fetch_5m_ohlc
from services.ticker_groups import get_tickers_game_5m

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

UPSERT_5M = """
INSERT INTO market_bars_5m (exchange, symbol, bar_start_utc, open, high, low, close, volume, source)
VALUES (:exchange, :symbol, :bar_start_utc, :open, :high, :low, :close, :volume, :source)
ON CONFLICT (exchange, symbol, bar_start_utc) DO UPDATE SET
  open = EXCLUDED.open,
  high = EXCLUDED.high,
  low = EXCLUDED.low,
  close = EXCLUDED.close,
  volume = EXCLUDED.volume,
  source = EXCLUDED.source,
  ingested_at = NOW()
"""

UPSERT_30M = """
INSERT INTO market_bars_30m (exchange, symbol, bar_start_utc, open, high, low, close, volume, source)
VALUES (:exchange, :symbol, :bar_start_utc, :open, :high, :low, :close, :volume, :source)
ON CONFLICT (exchange, symbol, bar_start_utc) DO UPDATE SET
  open = EXCLUDED.open,
  high = EXCLUDED.high,
  low = EXCLUDED.low,
  close = EXCLUDED.close,
  volume = EXCLUDED.volume,
  source = EXCLUDED.source,
  ingested_at = NOW()
"""


def _df_to_rows(df: pd.DataFrame, symbol: str, exchange: str) -> List[Dict[str, Any]]:
    sym = symbol.upper().strip()
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        dt = pd.Timestamp(row["datetime"])
        if dt.tzinfo is None:
            try:
                dt = dt.tz_localize("America/New_York", ambiguous="infer")
            except Exception:
                dt = dt.tz_localize("UTC", ambiguous="infer")
        bar_utc = dt.tz_convert("UTC").to_pydatetime()
        vol = row.get("Volume")
        if vol is None or (isinstance(vol, float) and np.isnan(vol)):
            vsql: Optional[int] = None
        else:
            try:
                vsql = int(vol)
            except (TypeError, ValueError):
                vsql = None
        rows.append(
            {
                "exchange": exchange,
                "symbol": sym,
                "bar_start_utc": bar_utc,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": vsql,
                "source": "yfinance",
            }
        )
    return rows


def _ingest_table(
    engine,
    sql: str,
    df: Optional[pd.DataFrame],
    symbol: str,
    exchange: str,
    label: str,
) -> int:
    if df is None or df.empty:
        logger.warning("%s: нет данных для %s", label, symbol)
        return 0
    rows = _df_to_rows(df, symbol, exchange)
    with engine.begin() as conn:
        for r in rows:
            conn.execute(text(sql), r)
    logger.info("%s %s: записано строк %d", label, symbol, len(rows))
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Загрузка 5m/30m в market_bars_*")
    p.add_argument("--days", type=int, default=7, help="Календарных дней назад (5m max %d)" % MAX_DAYS_5M)
    p.add_argument("--tickers", type=str, default="", help="Через запятую; иначе GAME_5M_TICKERS / TICKERS_FAST")
    p.add_argument("--exchange", type=str, default="US", help="Код биржи в PK (по умолчанию US)")
    p.add_argument("--5m-only", dest="m5_only", action="store_true", help="Только market_bars_5m")
    p.add_argument("--30m-only", dest="m30_only", action="store_true", help="Только market_bars_30m")
    p.add_argument("--ensure-tables", action="store_true", help="Выполнить DDL из 021_market_bars_5m_30m.sql")
    args = p.parse_args()

    days_5 = min(max(1, args.days), MAX_DAYS_5M)
    days_30 = min(max(1, args.days), MAX_DAYS_30M)

    if args.tickers.strip():
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = get_tickers_game_5m()
    if not tickers:
        raise SystemExit("Нет тикеров: задайте --tickers или GAME_5M_TICKERS / TICKERS_FAST в config.env")

    engine = create_engine(get_database_url())

    if args.ensure_tables:
        mig_path = project_root / "scripts" / "migrate_market_bars_intraday.py"
        spec = importlib.util.spec_from_file_location("migrate_market_bars_intraday", mig_path)
        if spec is None or spec.loader is None:
            raise SystemExit("Не удалось загрузить migrate_market_bars_intraday.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.apply_market_bars_intraday_schema(engine)
        logger.info("DDL market_bars_5m / market_bars_30m применён")

    do_5m = not args.m30_only
    do_30m = not args.m5_only

    total_5 = total_30 = 0
    for sym in tickers:
        if do_5m:
            df5 = fetch_5m_ohlc(sym, days=days_5)
            total_5 += _ingest_table(engine, UPSERT_5M, df5, sym, args.exchange, "5m")
        if do_30m:
            df30 = fetch_30m_ohlc(sym, days=days_30)
            total_30 += _ingest_table(engine, UPSERT_30M, df30, sym, args.exchange, "30m")

    print(f"Готово. 5m строк: {total_5}, 30m строк: {total_30}, тикеры: {', '.join(tickers)}")


if __name__ == "__main__":
    main()

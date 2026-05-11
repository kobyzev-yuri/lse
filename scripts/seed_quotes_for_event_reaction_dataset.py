#!/usr/bin/env python3
"""
Догрузка daily `quotes` для тикеров из `event_reaction_dataset`, которых ещё нет в БД.

`backfill_event_reaction_labeling.py` читает только таблицу `quotes`. Крон
`scripts/update_prices_cron.py` обновляет портфельные тикеры и те, что уже есть в
`quotes`, поэтому символы из KB (earnings) часто остаются без рядов → no_quotes.

Запуск из корня репозитория / в контейнере lse-bot:

  python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --dry-run
  python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --days 450 --limit 100

`--days` передаётся в yfinance как период backfill (нужно ≥ ~400 для окна
`event_reaction_labeling.load_quotes_window` + горизонты вперёд).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed quotes for event_reaction_dataset tickers missing from quotes")
    ap.add_argument("--dataset-version", type=str, default="v0")
    ap.add_argument(
        "--days",
        type=int,
        default=450,
        help="Сколько дней истории запросить у Yahoo на тикер (backfill)",
    )
    ap.add_argument("--limit", type=int, default=0, help="Макс. число тикеров (0 = без лимита)")
    ap.add_argument(
        "--all-symbols",
        action="store_true",
        help="Обновить все DISTINCT symbol из датасета, даже если в quotes уже есть ряды",
    )
    ap.add_argument("--dry-run", action="store_true", help="Только список тикеров, без yfinance/БД")
    args = ap.parse_args()

    from sqlalchemy import text

    from report_generator import get_engine
    from update_prices import update_all_prices

    engine = get_engine()
    lim_sql = ""
    params: dict = {"dv": args.dataset_version}
    if args.limit and args.limit > 0:
        lim_sql = "LIMIT :lim"
        params["lim"] = int(args.limit)

    if args.all_symbols:
        sql = f"""
            SELECT DISTINCT UPPER(TRIM(symbol)) AS sym
            FROM event_reaction_dataset
            WHERE dataset_version = :dv
              AND TRIM(COALESCE(symbol, '')) != ''
            ORDER BY 1
            {lim_sql}
        """
    else:
        sql = f"""
            SELECT DISTINCT UPPER(TRIM(e.symbol)) AS sym
            FROM event_reaction_dataset e
            WHERE e.dataset_version = :dv
              AND TRIM(COALESCE(e.symbol, '')) != ''
              AND NOT EXISTS (
                SELECT 1 FROM quotes q
                WHERE UPPER(TRIM(q.ticker)) = UPPER(TRIM(e.symbol))
                LIMIT 1
              )
            ORDER BY 1
            {lim_sql}
        """

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    tickers = [str(r[0]).strip() for r in rows if r and r[0]]

    if not tickers:
        logger.info("Нет тикеров для догрузки (все символы датасета уже имеют хотя бы одну строку в quotes; иначе используйте --all-symbols).")
        return 0

    logger.info("Тикеров к обработке: %s (all_symbols=%s)", len(tickers), args.all_symbols)
    if args.dry_run:
        for t in tickers[:50]:
            logger.info("  %s", t)
        if len(tickers) > 50:
            logger.info("  ... и ещё %s", len(tickers) - 50)
        return 0

    update_all_prices(tickers=tickers, force_days_back=max(30, int(args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

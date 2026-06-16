#!/usr/bin/env python3
"""
Догрузка daily `quotes` для тикеров из `event_reaction_dataset`, которых ещё нет в БД.

`backfill_event_reaction_labeling.py` читает только таблицу `quotes`. Крон
`scripts/update_prices_cron.py` обновляет портфельные тикеры и те, что уже есть в
`quotes`. По умолчанию этот скрипт берёт символы из датасета, пересечённые с **TICKERS_FAST/MEDIUM/LONG**; полный список символов в таблице: **`--include-all-dataset-symbols`**.

Запуск из корня репозитория / в контейнере lse-bot:

  python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --dry-run
  python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --all-symbols --days 450
  python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --min-quote-span-days 320 --days 450
  python scripts/seed_quotes_for_event_reaction_dataset.py --dataset-version v0 --include-all-dataset-symbols   # без фильтра конфига

`--days` передаётся в yfinance как период backfill (нужно ≥ ~400 для окна
`event_reaction_labeling.load_quotes_window` + горизонты вперёд).

По умолчанию догружаются только тикеры **без ни одной** строки в `quotes`. Если строки есть, но **короткая** история,
разметка даёт `no_quotes` на старых событиях — используйте **`--all-symbols`** (принудительно все символы датасета ∩ конфиг)
или **`--min-quote-span-days 320`**: догрузить, если MAX(date)−MIN(date) меньше порога.
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
    ap.add_argument(
        "--include-all-dataset-symbols",
        action="store_true",
        help="Не ограничивать символами из конфига (FAST+MEDIUM+LONG); все тикеры из event_reaction_dataset",
    )
    ap.add_argument(
        "--include-earnings-universe",
        action="store_true",
        help="Universe = earnings intelligence equities; с --all-symbols — все тикеры allowlist",
    )
    ap.add_argument(
        "--min-quote-span-days",
        type=int,
        default=0,
        help="Если >0: догрузить тикеры без quotes ИЛИ с (MAX(date)−MIN(date)) < порога (короткая история → no_quotes на старых событиях)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Только список тикеров, без yfinance/БД")
    args = ap.parse_args()

    from sqlalchemy import bindparam, text

    from report_generator import get_engine
    from services.ticker_groups import get_config_ticker_symbols_upper_unique
    from update_prices import update_all_prices

    engine = get_engine()

    if args.include_earnings_universe and args.all_symbols:
        from services.earnings_intelligence_universe import get_event_reaction_symbol_allowlist

        tickers = get_event_reaction_symbol_allowlist()
        if args.limit and args.limit > 0:
            tickers = tickers[: int(args.limit)]
        if not tickers:
            logger.info("Пустой earnings universe для seed quotes")
            return 0
        logger.info(
            "Тикеров к обработке: %s (include_earnings_universe + all_symbols, days=%s)",
            len(tickers),
            args.days,
        )
        if args.dry_run:
            for t in tickers:
                logger.info("  %s", t)
            return 0
        update_all_prices(tickers=tickers, force_days_back=max(30, int(args.days)))
        return 0

    lim_sql = ""
    params: dict = {"dv": args.dataset_version}
    cfg_filter = ""
    if args.include_earnings_universe:
        from services.earnings_intelligence_universe import get_event_reaction_symbol_allowlist

        eu_symbols = get_event_reaction_symbol_allowlist()
        if not eu_symbols:
            logger.error("Пустой earnings universe.")
            return 1
        cfg_filter = "AND UPPER(TRIM(e.symbol)) IN :sym"
        params["sym"] = eu_symbols
    elif not args.include_all_dataset_symbols:
        symbols = get_config_ticker_symbols_upper_unique()
        if not symbols:
            logger.error("Пустой список тикеров из конфига (TICKERS_FAST/MEDIUM/LONG).")
            return 1
        cfg_filter = "AND UPPER(TRIM(e.symbol)) IN :sym"
        params["sym"] = symbols

    if args.limit and args.limit > 0:
        lim_sql = "LIMIT :lim"
        params["lim"] = int(args.limit)

    if args.all_symbols:
        sql = f"""
            SELECT DISTINCT UPPER(TRIM(e.symbol)) AS sym
            FROM event_reaction_dataset e
            WHERE e.dataset_version = :dv
              AND TRIM(COALESCE(e.symbol, '')) != ''
              {cfg_filter}
            ORDER BY 1
            {lim_sql}
        """
    elif args.min_quote_span_days and args.min_quote_span_days > 0:
        params["min_span"] = int(args.min_quote_span_days)
        sql = f"""
            SELECT UPPER(TRIM(e.symbol)) AS sym
            FROM event_reaction_dataset e
            LEFT JOIN quotes q ON UPPER(TRIM(q.ticker)) = UPPER(TRIM(e.symbol))
            WHERE e.dataset_version = :dv
              AND TRIM(COALESCE(e.symbol, '')) != ''
              {cfg_filter}
            GROUP BY UPPER(TRIM(e.symbol))
            HAVING COUNT(q.id) = 0
                OR COALESCE(MAX(q.date::date) - MIN(q.date::date), 0) < :min_span
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
              {cfg_filter}
            ORDER BY 1
            {lim_sql}
        """

    stmt = text(sql)
    if cfg_filter:
        stmt = stmt.bindparams(bindparam("sym", expanding=True))

    with engine.connect() as conn:
        rows = conn.execute(stmt, params).fetchall()
    tickers = [str(r[0]).strip() for r in rows if r and r[0]]

    if not tickers:
        logger.info(
            "Нет тикеров для догрузки. Варианты: все уже с глубокой историей; нет пересечения с конфигом; "
            "или нужен принудительный backfill: --all-symbols или --min-quote-span-days 320 (см. docstring)."
        )
        return 0

    logger.info(
        "Тикеров к обработке: %s (all_symbols=%s, min_quote_span_days=%s)",
        len(tickers),
        args.all_symbols,
        args.min_quote_span_days or 0,
    )
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

#!/usr/bin/env python3
"""
Строки в event_reaction_dataset: skeleton из knowledge_base (EARNINGS) или статистика.

Полный feature builder (quotes, premarket, beta к NDX) — отдельный модуль позже.
По умолчанию в `event_reaction_dataset` попадают только earnings-строки KB с тикером из **TICKERS_FAST + TICKERS_MEDIUM + TICKERS_LONG** (`get_config_ticker_symbols_upper_unique`). Чтобы снова брать все тикеры из KB: **`--include-all-kb-tickers`**.

  python scripts/migrate_ml_event_analytics.py   # сначала DDL
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --dataset-version v0
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --kb-since 2026-02-01   # только события KB с этой даты (эра проекта)
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --include-all-kb-tickers   # все тикеры из KB (старое поведение)
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --include-earnings-universe   # конфиг ∪ earnings intelligence
  python scripts/backfill_event_reaction_labeling.py --dataset-version v0 --limit 500
  python scripts/build_event_reaction_dataset.py --stats
  python scripts/build_event_reaction_dataset.py --prune-non-config --dataset-version v0
  python scripts/build_event_reaction_dataset.py --prune-non-config --dataset-version v0 --dry-run

См. docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md §4.2
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_kb_since(s: str) -> Any:
    """ISO дата/время для фильтра kb.ts; без таймзоны — UTC."""
    import pandas as pd

    t = pd.Timestamp(str(s).strip())
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.to_pydatetime()


def _stats(engine) -> dict[str, Any]:
    from sqlalchemy import text

    out: dict[str, Any] = {}
    with engine.connect() as conn:
        try:
            n = conn.execute(text("SELECT COUNT(*) FROM event_reaction_dataset")).scalar()
            out["event_reaction_dataset_rows"] = int(n or 0)
        except Exception as e:
            out["error"] = str(e)
    return out


def _kb_base_where(*, past_only: bool) -> list[str]:
    where = [
        "UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'",
        "kb.ticker IS NOT NULL",
        "TRIM(kb.ticker) != ''",
        "LENGTH(TRIM(kb.ticker)) <= 16",
    ]
    if past_only:
        where.append("kb.ts::date <= CURRENT_DATE")
    return where


def _kb_from_clause(*, dedup_kb: bool, past_only: bool) -> str:
    """KB source: optional DISTINCT ON (ticker, event_date) to drop duplicate yfinance rows."""
    where = _kb_base_where(past_only=past_only)
    if not dedup_kb:
        return f"FROM knowledge_base kb WHERE {' AND '.join(where)}"
    return f"""
        FROM (
          SELECT DISTINCT ON (UPPER(TRIM(kb.ticker)), kb.ts::date)
            kb.id,
            kb.ticker,
            kb.ts,
            kb.event_type
          FROM knowledge_base kb
          WHERE {' AND '.join(where)}
          ORDER BY
            UPPER(TRIM(kb.ticker)),
            kb.ts::date,
            (CASE
              WHEN COALESCE(kb.content, '') ILIKE '%yfinance%'
                OR COALESCE(kb.link, '') ILIKE '%finance.yahoo%'
              THEN 1 ELSE 0
            END),
            (CASE WHEN EXISTS (
              SELECT 1 FROM earnings_event_detail ed WHERE ed.knowledge_base_id = kb.id
            ) THEN 0 ELSE 1 END),
            kb.id DESC
        ) kb
    """


def _prune_stale_skeletons(
    engine,
    *,
    dataset_version: str,
    dry_run: bool,
    symbol_allowlist: list[str] | None,
) -> int:
    """Drop kb_skeleton rows in the future or before first quote date (pre-IPO noise)."""
    from sqlalchemy import bindparam, text

    dv = (dataset_version or "v0").strip()
    sym_filter = ""
    params: dict = {"dv": dv}
    if symbol_allowlist is not None:
        if not symbol_allowlist:
            return 0
        sym_filter = "AND UPPER(TRIM(erd.symbol)) = ANY(:sym)"
        params["sym"] = symbol_allowlist

    count_sql = f"""
        SELECT COUNT(*) FROM event_reaction_dataset erd
        WHERE erd.dataset_version = :dv
          AND erd.label_source = 'kb_skeleton'
          {sym_filter}
          AND (
            erd.event_time_et::date > CURRENT_DATE
            OR erd.event_time_et::date < (
              SELECT MIN(q.date)::date
              FROM quotes q
              WHERE UPPER(TRIM(q.ticker)) = UPPER(TRIM(erd.symbol))
            )
          )
    """
    delete_sql = f"""
        DELETE FROM event_reaction_dataset erd
        WHERE erd.dataset_version = :dv
          AND erd.label_source = 'kb_skeleton'
          {sym_filter}
          AND (
            erd.event_time_et::date > CURRENT_DATE
            OR erd.event_time_et::date < (
              SELECT MIN(q.date)::date
              FROM quotes q
              WHERE UPPER(TRIM(q.ticker)) = UPPER(TRIM(erd.symbol))
            )
          )
    """
    count_stmt = text(count_sql)
    delete_stmt = text(delete_sql)
    if sym_filter:
        count_stmt = count_stmt.bindparams(bindparam("sym", expanding=True))
        delete_stmt = delete_stmt.bindparams(bindparam("sym", expanding=True))

    with engine.connect() as conn:
        n = int(conn.execute(count_stmt, params).scalar() or 0)
    if n == 0:
        logger.info("prune stale skeletons: nothing to remove (dataset_version=%s)", dv)
        return 0
    if dry_run:
        logger.info("dry-run prune stale skeletons: would delete %s rows", n)
        return 0
    with engine.begin() as conn:
        conn.execute(delete_stmt, params)
    logger.info("prune stale skeletons: deleted %s rows (dataset_version=%s)", n, dv)
    return 0


def _backfill_from_kb(
    engine,
    *,
    dataset_version: str,
    dry_run: bool,
    symbol_allowlist: list[str] | None,
    kb_since: Any | None,
    past_only: bool,
    dedup_kb: bool,
) -> int:
    from sqlalchemy import bindparam, text

    sym_filter = ""
    params: dict = {"dv": dataset_version}
    if symbol_allowlist is not None:
        if not symbol_allowlist:
            logger.error("Пустой symbol allowlist для --from-kb-earnings.")
            return 1
        sym_filter = "AND UPPER(TRIM(kb.ticker)) IN :sym"
        params["sym"] = symbol_allowlist
        logger.info("Фильтр: allowlist (%s шт.)", len(symbol_allowlist))
    else:
        logger.info("Фильтр по тикерам выключен (--include-all-kb-tickers): все подходящие тикеры KB")

    if past_only:
        logger.info("Фильтр: только прошедшие KB (kb.ts::date <= CURRENT_DATE)")
    if dedup_kb:
        logger.info("Фильтр: dedup KB по (ticker, event_date), prefer non-yfinance / earnings_event_detail")

    since_filter = ""
    if kb_since is not None:
        since_filter = "AND kb.ts >= :kb_since"
        params["kb_since"] = kb_since
        logger.info("Фильтр: только KB с kb.ts >= %s", kb_since)

    kb_from = _kb_from_clause(dedup_kb=dedup_kb, past_only=past_only)
    insert_sql = f"""
        INSERT INTO event_reaction_dataset (
            knowledge_base_id, symbol, event_time_et, event_type,
            features_before, outcomes_after, dataset_version, label_source
        )
        SELECT
            kb.id AS knowledge_base_id,
            TRIM(UPPER(kb.ticker)) AS symbol,
            (kb.ts AT TIME ZONE 'Europe/Moscow') AS event_time_et,
            COALESCE(NULLIF(TRIM(kb.event_type), ''), 'EARNINGS') AS event_type,
            '{{}}'::jsonb,
            '{{}}'::jsonb,
            :dv AS dataset_version,
            'kb_skeleton'
        {kb_from}
        WHERE 1=1
        {sym_filter}
        {since_filter}
        ON CONFLICT (symbol, event_time_et, event_type, dataset_version) DO NOTHING
    """
    stmt = text(insert_sql)
    if symbol_allowlist is not None:
        stmt = stmt.bindparams(bindparam("sym", expanding=True))

    if dry_run:
        count_q = f"SELECT COUNT(*) {kb_from} WHERE 1=1 {sym_filter} {since_filter}"
        count_stmt = text(count_q)
        if symbol_allowlist is not None:
            count_stmt = count_stmt.bindparams(bindparam("sym", expanding=True))
        with engine.connect() as conn:
            c = conn.execute(count_stmt, params).scalar()
        logger.info("dry-run: подходящих строк KB ≈ %s (вставки не выполнялись)", int(c or 0))
        return 0
    with engine.begin() as conn:
        r = conn.execute(stmt, params)
        try:
            ins = r.rowcount
        except Exception:
            ins = -1
        logger.info("INSERT завершён, rowcount=%s", ins)
    return 0


def _prune_non_config_symbols(
    engine,
    *,
    dataset_version: str,
    dry_run: bool,
    include_earnings_universe: bool,
) -> int:
    """Удалить строки event_reaction_dataset с symbol вне allowlist (конфиг или конфиг+earnings)."""
    from sqlalchemy import bindparam, text

    from services.earnings_intelligence_universe import get_event_reaction_symbol_allowlist
    from services.ticker_groups import get_config_ticker_symbols_upper_unique

    symbols = (
        get_event_reaction_symbol_allowlist()
        if include_earnings_universe
        else get_config_ticker_symbols_upper_unique()
    )
    if not symbols:
        logger.error("Пустой symbol allowlist для prune.")
        return 1
    dv = (dataset_version or "v0").strip()
    params: dict = {"dv": dv, "sym": symbols}
    count_sql = """
        SELECT COUNT(*) FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND UPPER(TRIM(symbol)) NOT IN :sym
    """
    delete_sql = """
        DELETE FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND UPPER(TRIM(symbol)) NOT IN :sym
    """
    count_stmt = text(count_sql).bindparams(bindparam("sym", expanding=True))
    delete_stmt = text(delete_sql).bindparams(bindparam("sym", expanding=True))
    with engine.connect() as conn:
        n = int(conn.execute(count_stmt, params).scalar() or 0)
    if n == 0:
        logger.info("prune: лишних строк нет (dataset_version=%s, конфиг-тикеров=%s)", dv, len(symbols))
        return 0
    if dry_run:
        logger.info(
            "dry-run prune: удалило бы %s строк (dataset_version=%s, останется только %s конфиг-тикеров)",
            n,
            dv,
            len(symbols),
        )
        return 0
    with engine.begin() as conn:
        r = conn.execute(delete_stmt, params)
        try:
            deleted = int(r.rowcount or 0)
        except Exception:
            deleted = n
    logger.info(
        "prune: удалено %s строк (dataset_version=%s); universe = конфиг (%s тикеров)",
        deleted,
        dv,
        len(symbols),
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="event_reaction_dataset: skeleton / stats")
    ap.add_argument("--stats", action="store_true", help="Только COUNT(*) в event_reaction_dataset")
    ap.add_argument(
        "--prune-non-config",
        action="store_true",
        help="Удалить строки с symbol вне TICKERS_FAST/MEDIUM/LONG (см. --dataset-version)",
    )
    ap.add_argument("--from-kb-earnings", action="store_true", help="Вставить skeleton-строки из KB (EARNINGS*)")
    ap.add_argument(
        "--include-all-kb-tickers",
        action="store_true",
        help="Не фильтровать по конфигу: все earnings-тикеры из KB (старое поведение)",
    )
    ap.add_argument(
        "--include-earnings-universe",
        action="store_true",
        help="Allowlist = TICKERS_FAST/MEDIUM/LONG ∪ earnings intelligence equities (ANET, GOOGL, …)",
    )
    ap.add_argument("--dataset-version", type=str, default="v0", help="dataset_version для вставок")
    ap.add_argument("--dry-run", action="store_true", help="Не писать в БД (только лог для --from-kb-earnings)")
    ap.add_argument(
        "--kb-since",
        type=str,
        default="",
        help="Только строки KB с kb.ts >= даты (ISO, напр. 2026-02-01). Пусто — из EVENT_REACTION_KB_SINCE в config или без фильтра",
    )
    ap.add_argument(
        "--past-only",
        action="store_true",
        help="Только KB с kb.ts::date <= CURRENT_DATE (default with --include-earnings-universe)",
    )
    ap.add_argument(
        "--no-past-only",
        action="store_true",
        help="Отключить past-only даже с --include-earnings-universe",
    )
    ap.add_argument(
        "--dedup-kb",
        action="store_true",
        help="Один KB row на (ticker, event_date); prefer non-yfinance (default with --include-earnings-universe)",
    )
    ap.add_argument(
        "--no-dedup-kb",
        action="store_true",
        help="Отключить dedup KB",
    )
    ap.add_argument(
        "--prune-stale-skeletons",
        action="store_true",
        help="Удалить kb_skeleton: future events и pre-IPO (до MIN(quotes.date))",
    )
    args = ap.parse_args()

    from config_loader import get_config_value
    from report_generator import get_engine

    kb_since_raw = (args.kb_since or "").strip() or (get_config_value("EVENT_REACTION_KB_SINCE", "") or "").strip()
    kb_since_dt = None
    if kb_since_raw:
        try:
            kb_since_dt = _parse_kb_since(kb_since_raw)
        except Exception as e:
            logger.error("Некорректный --kb-since / EVENT_REACTION_KB_SINCE: %s", e)
            return 1

    engine = get_engine()
    if args.stats:
        print(_stats(engine))
        return 0
    if args.prune_non_config:
        return _prune_non_config_symbols(
            engine,
            dataset_version=args.dataset_version.strip() or "v0",
            dry_run=bool(args.dry_run),
            include_earnings_universe=bool(args.include_earnings_universe),
        )
    if args.from_kb_earnings:
        if args.include_all_kb_tickers and args.include_earnings_universe:
            logger.error("Укажите только один из --include-all-kb-tickers / --include-earnings-universe")
            return 1
        allowlist: list[str] | None
        if args.include_all_kb_tickers:
            allowlist = None
        elif args.include_earnings_universe:
            from services.earnings_intelligence_universe import get_event_reaction_symbol_allowlist

            allowlist = get_event_reaction_symbol_allowlist()
        else:
            from services.ticker_groups import get_config_ticker_symbols_upper_unique

            allowlist = get_config_ticker_symbols_upper_unique()

        earnings_defaults = bool(args.include_earnings_universe)
        past_only = bool(args.past_only or (earnings_defaults and not args.no_past_only))
        dedup_kb = bool(args.dedup_kb or (earnings_defaults and not args.no_dedup_kb))
        prune_stale = bool(args.prune_stale_skeletons or earnings_defaults)

        rc = _backfill_from_kb(
            engine,
            dataset_version=args.dataset_version.strip() or "v0",
            dry_run=bool(args.dry_run),
            symbol_allowlist=allowlist,
            kb_since=kb_since_dt,
            past_only=past_only,
            dedup_kb=dedup_kb,
        )
        if rc != 0:
            return rc
        if prune_stale:
            return _prune_stale_skeletons(
                engine,
                dataset_version=args.dataset_version.strip() or "v0",
                dry_run=bool(args.dry_run),
                symbol_allowlist=allowlist,
            )
        return 0
    logger.error("Укажите --stats, --prune-non-config или --from-kb-earnings")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

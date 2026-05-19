#!/usr/bin/env python3
"""
Строки в event_reaction_dataset: skeleton из knowledge_base (EARNINGS) или статистика.

Полный feature builder (quotes, premarket, beta к NDX) — отдельный модуль позже.
По умолчанию в `event_reaction_dataset` попадают только earnings-строки KB с тикером из **TICKERS_FAST + TICKERS_MEDIUM + TICKERS_LONG** (`get_config_ticker_symbols_upper_unique`). Чтобы снова брать все тикеры из KB: **`--include-all-kb-tickers`**.

  python scripts/migrate_ml_event_analytics.py   # сначала DDL
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --dataset-version v0
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --kb-since 2026-02-01   # только события KB с этой даты (эра проекта)
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --include-all-kb-tickers   # все тикеры из KB (старое поведение)
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


def _backfill_from_kb(
    engine,
    *,
    dataset_version: str,
    dry_run: bool,
    config_tickers_only: bool,
    kb_since: Any | None,
) -> int:
    from sqlalchemy import bindparam, text

    from services.ticker_groups import get_config_ticker_symbols_upper_unique

    base_where = """
        UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
          AND kb.ticker IS NOT NULL
          AND TRIM(kb.ticker) != ''
          AND LENGTH(TRIM(kb.ticker)) <= 16
    """
    sym_filter = ""
    params: dict = {"dv": dataset_version}
    if config_tickers_only:
        symbols = get_config_ticker_symbols_upper_unique()
        if not symbols:
            logger.error("Пустой список тикеров из конфига (TICKERS_FAST/MEDIUM/LONG).")
            return 1
        sym_filter = "AND UPPER(TRIM(kb.ticker)) IN :sym"
        params["sym"] = symbols
        logger.info("Фильтр: только тикеры из конфига (%s шт.)", len(symbols))
    else:
        logger.info("Фильтр по конфигу выключен (--include-all-kb-tickers): все подходящие тикеры KB")

    since_filter = ""
    if kb_since is not None:
        since_filter = "AND kb.ts >= :kb_since"
        params["kb_since"] = kb_since
        logger.info("Фильтр: только KB с kb.ts >= %s", kb_since)

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
        FROM knowledge_base kb
        WHERE {base_where}
        {sym_filter}
        {since_filter}
        ON CONFLICT (symbol, event_time_et, event_type, dataset_version) DO NOTHING
    """
    stmt = text(insert_sql)
    if config_tickers_only:
        stmt = stmt.bindparams(bindparam("sym", expanding=True))

    if dry_run:
        count_q = f"SELECT COUNT(*) FROM knowledge_base kb WHERE {base_where} {sym_filter} {since_filter}"
        count_stmt = text(count_q)
        if config_tickers_only:
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
) -> int:
    """Удалить строки event_reaction_dataset с symbol вне TICKERS_FAST/MEDIUM/LONG."""
    from sqlalchemy import bindparam, text

    from services.ticker_groups import get_config_ticker_symbols_upper_unique

    symbols = get_config_ticker_symbols_upper_unique()
    if not symbols:
        logger.error("Пустой список тикеров из конфига (TICKERS_FAST/MEDIUM/LONG).")
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
    ap.add_argument("--dataset-version", type=str, default="v0", help="dataset_version для вставок")
    ap.add_argument("--dry-run", action="store_true", help="Не писать в БД (только лог для --from-kb-earnings)")
    ap.add_argument(
        "--kb-since",
        type=str,
        default="",
        help="Только строки KB с kb.ts >= даты (ISO, напр. 2026-02-01). Пусто — из EVENT_REACTION_KB_SINCE в config или без фильтра",
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
        )
    if args.from_kb_earnings:
        return _backfill_from_kb(
            engine,
            dataset_version=args.dataset_version.strip() or "v0",
            dry_run=bool(args.dry_run),
            config_tickers_only=not bool(args.include_all_kb_tickers),
            kb_since=kb_since_dt,
        )
    logger.error("Укажите --stats, --prune-non-config или --from-kb-earnings")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

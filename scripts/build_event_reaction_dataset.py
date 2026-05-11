#!/usr/bin/env python3
"""
Строки в event_reaction_dataset: skeleton из knowledge_base (EARNINGS) или статистика.

Полный feature builder (quotes, premarket, beta к NDX) — отдельный модуль позже.
Сейчас: дедуп по UNIQUE(symbol, event_time_et, event_type, dataset_version).

  python scripts/migrate_ml_event_analytics.py   # сначала DDL
  python scripts/build_event_reaction_dataset.py --from-kb-earnings --dataset-version v0
  python scripts/backfill_event_reaction_labeling.py --dataset-version v0 --limit 500
  python scripts/build_event_reaction_dataset.py --stats

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


def _backfill_from_kb(engine, *, dataset_version: str, dry_run: bool) -> int:
    from sqlalchemy import text

    sql = text(
        """
        INSERT INTO event_reaction_dataset (
            knowledge_base_id, symbol, event_time_et, event_type,
            features_before, outcomes_after, dataset_version, label_source
        )
        SELECT
            kb.id AS knowledge_base_id,
            TRIM(UPPER(kb.ticker)) AS symbol,
            (kb.ts AT TIME ZONE 'Europe/Moscow') AS event_time_et,
            COALESCE(NULLIF(TRIM(kb.event_type), ''), 'EARNINGS') AS event_type,
            '{}'::jsonb,
            '{}'::jsonb,
            :dv AS dataset_version,
            'kb_skeleton'
        FROM knowledge_base kb
        WHERE UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
          AND kb.ticker IS NOT NULL
          AND TRIM(kb.ticker) != ''
          AND LENGTH(TRIM(kb.ticker)) <= 16
        ON CONFLICT (symbol, event_time_et, event_type, dataset_version) DO NOTHING
        """
    )
    if dry_run:
        with engine.connect() as conn:
            c = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM knowledge_base kb
                    WHERE UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
                      AND kb.ticker IS NOT NULL AND TRIM(kb.ticker) != ''
                    """
                )
            ).scalar()
        logger.info("dry-run: подходящих строк KB ≈ %s (вставки не выполнялись)", int(c or 0))
        return 0
    with engine.begin() as conn:
        r = conn.execute(sql, {"dv": dataset_version})
        try:
            ins = r.rowcount
        except Exception:
            ins = -1
        logger.info("INSERT завершён, rowcount=%s", ins)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="event_reaction_dataset: skeleton / stats")
    ap.add_argument("--stats", action="store_true", help="Только COUNT(*) в event_reaction_dataset")
    ap.add_argument("--from-kb-earnings", action="store_true", help="Вставить skeleton-строки из KB (EARNINGS*)")
    ap.add_argument("--dataset-version", type=str, default="v0", help="dataset_version для вставок")
    ap.add_argument("--dry-run", action="store_true", help="Не писать в БД (только лог для --from-kb-earnings)")
    args = ap.parse_args()

    from report_generator import get_engine

    engine = get_engine()
    if args.stats:
        print(_stats(engine))
        return 0
    if args.from_kb_earnings:
        return _backfill_from_kb(engine, dataset_version=args.dataset_version.strip() or "v0", dry_run=bool(args.dry_run))
    logger.error("Укажите --stats или --from-kb-earnings")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

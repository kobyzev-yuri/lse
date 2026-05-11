#!/usr/bin/env python3
"""
Авторазметка event_reaction_dataset из daily quotes (MVP).

Логика: services/event_reaction_labeling.py (feature_builder_version=quotes_mvp_1,
outcome_builder_version=quotes_fwd_1, label_source=auto_quotes_v1).

Примеры:
  python scripts/backfill_event_reaction_labeling.py --dataset-version v0 --limit 500
  python scripts/backfill_event_reaction_labeling.py --dry-run --limit 20
  python scripts/backfill_event_reaction_labeling.py --only-features --limit 2000
  python scripts/backfill_event_reaction_labeling.py --only-outcomes --limit 5000
  python scripts/backfill_event_reaction_labeling.py --force-outcomes --limit 100
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_horizons(s: str) -> Tuple[int, ...]:
    parts = [p.strip() for p in (s or "").split(",") if p.strip()]
    if not parts:
        return (1, 5, 20)
    out: List[int] = []
    for p in parts:
        n = int(p, 10)
        if n < 1:
            raise ValueError(f"horizon must be >= 1, got {p}")
        out.append(n)
    return tuple(out)


def _parse_ts(s: str) -> Any:
    import pandas as pd

    t = pd.Timestamp(s)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.to_pydatetime()


def _row_from_db(r: Any) -> Dict[str, Any]:
    return {
        "id": r[0],
        "symbol": r[1],
        "event_time_et": r[2],
        "features_before": r[3],
        "outcomes_after": r[4],
    }


def _apply_update(conn, row_id: int, upd: Dict[str, Any], dumps_fn) -> None:
    from sqlalchemy import text

    sets: List[str] = ["updated_at = NOW()"]
    params: Dict[str, Any] = {"id": row_id}
    if "features_before" in upd:
        sets.append("features_before = CAST(:fb AS jsonb)")
        params["fb"] = dumps_fn(upd["features_before"])
    if "outcomes_after" in upd:
        sets.append("outcomes_after = CAST(:oa AS jsonb)")
        params["oa"] = dumps_fn(upd["outcomes_after"])
    if "final_label" in upd:
        sets.append("final_label = :fl")
        params["fl"] = upd["final_label"]
    if "label_source" in upd:
        sets.append("label_source = :ls")
        params["ls"] = upd["label_source"]
    sql = f"UPDATE event_reaction_dataset SET {', '.join(sets)} WHERE id = :id"
    conn.execute(text(sql), params)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill event_reaction_dataset labeling from quotes")
    ap.add_argument("--dataset-version", type=str, default="v0", help="Filter dataset_version")
    ap.add_argument("--limit", type=int, default=500, help="Max rows to process")
    ap.add_argument("--dry-run", action="store_true", help="No COMMIT")
    ap.add_argument("--only-features", action="store_true", help="Only fill features_before")
    ap.add_argument("--only-outcomes", action="store_true", help="Only fill outcomes_after + label")
    ap.add_argument("--force-features", action="store_true", help="Overwrite non-empty features_before")
    ap.add_argument("--force-outcomes", action="store_true", help="Overwrite non-empty outcomes_after (+ label)")
    ap.add_argument("--horizons", type=str, default="1,5,20", help="Forward horizons, e.g. 1,5,20")
    ap.add_argument("--id-from", type=int, default=0, help="Only id >= this (0 = no bound)")
    ap.add_argument("--id-to", type=int, default=0, help="Only id <= this (0 = no bound)")
    ap.add_argument("--since", type=str, default="", help="Only event_time_et >= (ISO datetime)")
    ap.add_argument("--until", type=str, default="", help="Only event_time_et < (ISO datetime)")
    args = ap.parse_args()

    if args.only_features and args.only_outcomes:
        ap.error("use only one of --only-features / --only-outcomes")
    do_features = not args.only_outcomes
    do_outcomes = not args.only_features

    try:
        horizons = _parse_horizons(args.horizons)
    except ValueError as e:
        logger.error("%s", e)
        return 1

    since_ts: Optional[Any] = None
    until_ts: Optional[Any] = None
    if args.since.strip():
        try:
            since_ts = _parse_ts(args.since.strip())
        except Exception as e:
            logger.error("bad --since: %s", e)
            return 1
    if args.until.strip():
        try:
            until_ts = _parse_ts(args.until.strip())
        except Exception as e:
            logger.error("bad --until: %s", e)
            return 1

    from sqlalchemy import text

    from report_generator import get_engine
    from services.event_reaction_labeling import json_dumps_obj, labeling_updates_for_row

    engine = get_engine()

    sql = """
        SELECT id, symbol, event_time_et, features_before, outcomes_after
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND (
            (:do_f AND (:ff OR features_before = '{}'::jsonb))
            OR (:do_o AND (:fo OR outcomes_after = '{}'::jsonb))
          )
          AND (:id_from = 0 OR id >= :id_from)
          AND (:id_to = 0 OR id <= :id_to)
          AND (:since_ts IS NULL OR event_time_et >= :since_ts)
          AND (:until_ts IS NULL OR event_time_et < :until_ts)
        ORDER BY id
        LIMIT :lim
    """

    params = {
        "dv": args.dataset_version,
        "do_f": do_features,
        "do_o": do_outcomes,
        "ff": args.force_features,
        "fo": args.force_outcomes,
        "id_from": int(args.id_from),
        "id_to": int(args.id_to),
        "since_ts": since_ts,
        "until_ts": until_ts,
        "lim": max(1, int(args.limit)),
    }

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    if not rows:
        logger.info("Нет строк для обработки (фильтр / уже заполнено).")
        return 0

    updated = 0
    skipped = 0
    partial_notes = 0

    for r in rows:
        d = _row_from_db(r)
        rid = int(d["id"])
        upd, note = labeling_updates_for_row(
            d,
            do_features=do_features,
            do_outcomes=do_outcomes,
            force_features=args.force_features,
            force_outcomes=args.force_outcomes,
            horizons=horizons,
        )
        if not upd:
            skipped += 1
            logger.debug("skip id=%s note=%s", rid, note)
            continue
        if note:
            partial_notes += 1
            logger.warning("id=%s partial/warn: %s keys=%s", rid, note, list(upd.keys()))
        if args.dry_run:
            logger.info("dry-run id=%s keys=%s", rid, list(upd.keys()))
            updated += 1
            continue
        with engine.begin() as conn:
            _apply_update(conn, rid, upd, json_dumps_obj)
        updated += 1

    logger.info(
        "Готово: candidates=%s updated/would=%s skipped_empty=%s partial_warnings=%s dry_run=%s",
        len(rows),
        updated,
        skipped,
        partial_notes,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

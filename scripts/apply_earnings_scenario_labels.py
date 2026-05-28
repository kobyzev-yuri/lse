#!/usr/bin/env python3
"""
Apply LLM scenario_hints from earnings_event_detail to event_reaction_dataset.final_label.

Does not overwrite rows with label_source='manual'.

Examples:
  python scripts/apply_earnings_scenario_labels.py --dry-run
  python scripts/apply_earnings_scenario_labels.py --symbols META,NVDA
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

LABEL_SOURCE = "llm_scenario_v0"
CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _top_scenario(guidance_summary: dict | None) -> str | None:
    if not guidance_summary:
        return None
    hints = guidance_summary.get("scenario_hints")
    if not isinstance(hints, list):
        return None
    candidates = [h for h in hints if isinstance(h, dict) and h.get("scenario")]
    if not candidates:
        return None
    candidates.sort(key=lambda h: CONF_RANK.get(str(h.get("confidence") or "").lower(), 9))
    return str(candidates[0]["scenario"]).strip()


def _load_rows(engine, *, symbols: set[str] | None, dataset_version: str) -> list[dict[str, Any]]:
    where = ["ed.guidance_summary ? 'scenario_hints'"]
    params: dict[str, Any] = {"dataset_version": dataset_version}
    if symbols:
        where.append("UPPER(TRIM(kb.ticker)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    q = text(
        f"""
        SELECT
          erd.id AS dataset_id,
          erd.knowledge_base_id,
          erd.symbol,
          erd.final_label,
          erd.label_source,
          ed.guidance_summary
        FROM earnings_event_detail ed
        JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
        JOIN event_reaction_dataset erd ON erd.knowledge_base_id = ed.knowledge_base_id
        WHERE {' AND '.join(where)}
          AND erd.dataset_version = :dataset_version
        ORDER BY kb.ticker, kb.ts
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply LLM scenario hints to event_reaction_dataset")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default="", help="Comma-separated tickers")
    ap.add_argument("--dataset-version", default="v0_expanded_baseline")
    ap.add_argument("--force", action="store_true", help="Overwrite non-manual labels")
    args = ap.parse_args()

    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    engine = get_engine()
    rows = _load_rows(engine, symbols=symbols, dataset_version=args.dataset_version.strip())
    if not rows:
        logger.info("No rows with scenario_hints + event_reaction_dataset match")
        return 0

    updated = 0
    skipped = 0
    with engine.begin() as conn:
        for row in rows:
            gs = row.get("guidance_summary")
            if isinstance(gs, str):
                try:
                    gs = json.loads(gs)
                except json.JSONDecodeError:
                    gs = {}
            scenario = _top_scenario(gs if isinstance(gs, dict) else None)
            if not scenario:
                skipped += 1
                continue
            if row.get("label_source") == "manual" and not args.force:
                logger.info("skip manual id=%s %s", row.get("dataset_id"), row.get("symbol"))
                skipped += 1
                continue
            if row.get("final_label") == scenario and row.get("label_source") == LABEL_SOURCE:
                skipped += 1
                continue
            logger.info(
                "apply id=%s %s: %s -> %s",
                row.get("dataset_id"),
                row.get("symbol"),
                row.get("final_label"),
                scenario,
            )
            if not args.dry_run:
                conn.execute(
                    text(
                        """
                        UPDATE event_reaction_dataset
                        SET final_label = :label,
                            label_source = :label_source,
                            updated_at = NOW()
                        WHERE id = :dataset_id
                        """
                    ),
                    {
                        "label": scenario,
                        "label_source": LABEL_SOURCE,
                        "dataset_id": int(row["dataset_id"]),
                    },
                )
            updated += 1

    logger.info("%s updated=%s skipped=%s", "Would update" if args.dry_run else "Updated", updated, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

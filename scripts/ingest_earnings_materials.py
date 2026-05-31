#!/usr/bin/env python3
"""
Download and parse rows from earnings_material (hybrid ingest v0).

Reads registry rows with parse_status=registered (default), fetches source_url,
stores raw bytes on disk, extracts plain text for HTML pages, updates DB fields.

Examples:
  python scripts/ingest_earnings_materials.py --dry-run --limit 5
  python scripts/ingest_earnings_materials.py --ensure-table --symbol SNDK --limit 1
  python scripts/ingest_earnings_materials.py --status registered,failed --force
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import date
from typing import Any

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_material_parser import (  # noqa: E402
    fetch_url,
    parse_fetched_content,
    save_raw_copy,
    storage_path,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_STORE_DIR = project_root / "logs" / "earnings_materials"


def _ensure_table(engine) -> None:
    sql_path = project_root / "scripts" / "sql" / "ml_event_analytics_schema.sql"
    raw = sql_path.read_text(encoding="utf-8")
    buf: list[str] = []
    with engine.begin() as conn:
        for line in raw.splitlines():
            if line.strip().startswith("--"):
                continue
            buf.append(line)
            if line.strip().endswith(";"):
                stmt = "\n".join(buf).strip()
                buf = []
                if stmt:
                    conn.execute(text(stmt))


def _load_rows(
    engine,
    *,
    statuses: tuple[str, ...],
    symbol: str | None,
    material_id: int | None,
    limit: int,
    force: bool,
    pending_event_keys: set[tuple[str, date]] | None = None,
) -> list[dict[str, Any]]:
    if pending_event_keys is not None and not pending_event_keys:
        return []
    where = ["1=1"]
    params: dict[str, Any] = {"limit": int(limit)}
    if not force:
        where.append("parse_status = ANY(:statuses)")
        params["statuses"] = list(statuses)
    if symbol:
        where.append("UPPER(TRIM(symbol)) = :symbol")
        params["symbol"] = symbol.strip().upper()
    if material_id:
        where.append("id = :material_id")
        params["material_id"] = int(material_id)
    if pending_event_keys is not None:
        pairs = sorted(pending_event_keys)
        where.append(
            "(UPPER(TRIM(symbol)), event_date) IN ("
            + ", ".join(f"(:sym_{i}, :dt_{i})" for i in range(len(pairs)))
            + ")"
        )
        for i, (sym, ev_date) in enumerate(pairs):
            params[f"sym_{i}"] = sym
            params[f"dt_{i}"] = ev_date
    q = text(
        f"""
        SELECT
          id, symbol, event_date, fiscal_period, material_type,
          source_name, source_url, title, parse_status, meta
        FROM earnings_material
        WHERE {' AND '.join(where)}
        ORDER BY event_date DESC NULLS LAST, id ASC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


def _update_row(
    engine,
    *,
    material_id: int,
    local_path: str | None,
    content_sha256: str | None,
    content_text: str | None,
    parse_status: str,
    parse_error: str | None,
    meta_patch: dict[str, Any],
) -> None:
    q = text(
        """
        UPDATE earnings_material
        SET
          local_path = COALESCE(:local_path, local_path),
          content_sha256 = COALESCE(:content_sha256, content_sha256),
          content_text = CASE
            WHEN :content_text IS NULL THEN content_text
            ELSE :content_text
          END,
          parse_status = :parse_status,
          parse_error = :parse_error,
          meta = COALESCE(meta, '{}'::jsonb) || CAST(:meta_patch AS jsonb),
          updated_at = NOW()
        WHERE id = :material_id
        """
    )
    with engine.begin() as conn:
        conn.execute(
            q,
            {
                "material_id": int(material_id),
                "local_path": local_path,
                "content_sha256": content_sha256,
                "content_text": content_text,
                "parse_status": parse_status,
                "parse_error": parse_error,
                "meta_patch": json.dumps(meta_patch, ensure_ascii=False),
            },
        )


def _process_row(
    row: dict[str, Any],
    *,
    store_dir: Path,
    dry_run: bool,
    timeout_sec: int,
) -> dict[str, Any]:
    material_id = int(row["id"])
    url = str(row["source_url"])
    symbol = str(row["symbol"])
    logger.info("material id=%s symbol=%s type=%s url=%s", material_id, symbol, row.get("material_type"), url)

    if dry_run:
        return {"id": material_id, "dry_run": True, "url": url}

    fetched = fetch_url(url, timeout_sec=timeout_sec)
    parsed = parse_fetched_content(fetched)
    out_path = storage_path(
        store_dir,
        symbol=symbol,
        material_id=material_id,
        digest=parsed.content_sha256,
        ext=parsed.raw_ext,
    )
    save_raw_copy(out_path, fetched.content)

    meta_patch = {
        "fetch": {
            "final_url": parsed.final_url,
            "content_type": parsed.content_type,
            "method": parsed.method,
        }
    }
    if parsed.discovered_links:
        meta_patch["discovered_links"] = list(parsed.discovered_links[:40])

    if parsed.text:
        parse_status = "parsed"
    elif parsed.parse_error and parsed.parse_error.startswith("pdf_"):
        parse_status = "downloaded"
    else:
        parse_status = "failed"

    if not dry_run:
        _update_row(
            engine=get_engine(),
            material_id=material_id,
            local_path=str(out_path),
            content_sha256=parsed.content_sha256,
            content_text=parsed.text or None,
            parse_status=parse_status,
            parse_error=parsed.parse_error,
            meta_patch=meta_patch,
        )

    return {
        "id": material_id,
        "parse_status": parse_status,
        "text_chars": len(parsed.text),
        "discovered_links": len(parsed.discovered_links),
        "local_path": str(out_path),
        "parse_error": parsed.parse_error,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch and parse earnings_material registry rows")
    ap.add_argument("--ensure-table", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Ignore parse_status filter")
    ap.add_argument("--status", default="registered", help="Comma-separated parse_status values")
    ap.add_argument("--symbol", default="", help="Only one ticker, e.g. SNDK")
    ap.add_argument("--id", type=int, default=0, help="Only one earnings_material.id")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--timeout-sec", type=int, default=45)
    ap.add_argument("--store-dir", default=str(DEFAULT_STORE_DIR))
    ap.add_argument(
        "--new-events-only",
        action="store_true",
        help="Only download materials for calendar events without LLM extraction",
    )
    ap.add_argument("--since", default="2026-01-01", help="With --new-events-only, KB events on/after date")
    args = ap.parse_args()

    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    if not statuses and not args.force:
        logger.error("No statuses provided")
        return 1

    engine = get_engine()
    if args.ensure_table:
        _ensure_table(engine)

    pending_keys: set[tuple[str, date]] | None = None
    if args.new_events_only:
        from datetime import datetime

        from services.earnings_calendar_new_events import load_pending_calendar_events, pending_event_keys

        since_d = datetime.strptime(args.since.strip()[:10], "%Y-%m-%d").date()
        sym_set = {args.symbol.strip().upper()} if args.symbol.strip() else None
        pending = load_pending_calendar_events(engine, since=since_d, symbols=sym_set, limit=max(1, args.limit * 4))
        pending_keys = pending_event_keys(pending)
        logger.info("New-events-only ingest: pending calendar events=%s", len(pending_keys))

    rows = _load_rows(
        engine,
        statuses=statuses or ("registered",),
        symbol=args.symbol or None,
        material_id=args.id or None,
        limit=max(1, args.limit),
        force=args.force,
        pending_event_keys=pending_keys,
    )
    if not rows:
        logger.info("No earnings_material rows to process")
        return 0

    if args.dry_run:
        results = [
            _process_row(r, store_dir=Path(args.store_dir), dry_run=True, timeout_sec=args.timeout_sec)
            for r in rows
        ]
        logger.info("Dry-run checked %s rows: %s", len(results), results)
        return 0

    results = []
    for row in rows:
        try:
            results.append(
                _process_row(
                    row,
                    store_dir=Path(args.store_dir),
                    dry_run=False,
                    timeout_sec=args.timeout_sec,
                )
            )
        except Exception as e:
            logger.exception("Failed material id=%s: %s", row.get("id"), e)
            _update_row(
                engine=engine,
                material_id=int(row["id"]),
                local_path=None,
                content_sha256=None,
                content_text=None,
                parse_status="failed",
                parse_error=str(e)[:500],
                meta_patch={"fetch": {"error": str(e)[:500]}},
            )
            results.append({"id": row.get("id"), "parse_status": "failed", "parse_error": str(e)})

    ok = sum(1 for r in results if r.get("parse_status") in ("parsed", "downloaded"))
    logger.info("Processed %s rows; parsed/downloaded=%s; details=%s", len(results), ok, results)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

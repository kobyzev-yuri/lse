#!/usr/bin/env python3
"""
Probe earnings document sources for calendar events (SEC, Fool, catalog, SEC exhibits).

Use when autoprep left only thin 8-K or empty scenario_hints.

Examples:
  python scripts/discover_earnings_material_sources.py --symbol NBIS --event-date 2026-05-13
  python scripts/discover_earnings_material_sources.py --symbols AMD,AMZN,CIEN --since 2026-01-01
  python scripts/discover_earnings_material_sources.py --symbol NBIS --event-date 2026-05-13 --register
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_material_auto_sources import (  # noqa: E402
    auto_materials_for_event,
    fool_rate_limit_active,
)
from services.earnings_material_catalog import catalog_for_event  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def _load_pending(engine, *, symbols: set[str] | None, since: date) -> list[tuple[str, date]]:
    from sqlalchemy import text

    where = [
        "kb.ts::date >= :since",
        "UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'",
    ]
    params: dict = {"since": since}
    if symbols:
        where.append("UPPER(TRIM(kb.ticker)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    q = text(
        f"""
        SELECT DISTINCT UPPER(TRIM(kb.ticker)) AS symbol, kb.ts::date AS event_date
        FROM knowledge_base kb
        LEFT JOIN earnings_event_detail ed ON ed.knowledge_base_id = kb.id
        WHERE {' AND '.join(where)}
          AND (
            ed.id IS NULL
            OR jsonb_array_length(COALESCE(ed.guidance_summary->'scenario_hints', '[]'::jsonb)) = 0
          )
        ORDER BY kb.ts::date DESC, symbol
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [(str(r["symbol"]), r["event_date"]) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover earnings material URLs for events")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--event-date", default="")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--register", action="store_true", help="Upsert into earnings_material via sync")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    sym_set: set[str] | None = None
    if args.symbol.strip():
        sym_set = {args.symbol.strip().upper()}
    elif args.symbols.strip():
        sym_set = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}

    engine = get_engine()
    events: list[tuple[str, date]] = []
    if args.event_date.strip() and sym_set and len(sym_set) == 1:
        events = [(next(iter(sym_set)), _parse_date(args.event_date))]
    else:
        events = _load_pending(engine, symbols=sym_set, since=_parse_date(args.since))

    report: list[dict] = []
    print(f"Fool cooldown active: {fool_rate_limit_active()}")
    for sym, ev_d in events:
        catalog = catalog_for_event(sym, ev_d)
        auto = auto_materials_for_event(sym, ev_d)
        row = {
            "symbol": sym,
            "event_date": ev_d.isoformat(),
            "catalog_n": len(catalog),
            "auto_n": len(auto),
            "catalog_urls": [c.source_url for c in catalog],
            "auto_urls": [
                {"type": c.material_type, "url": c.source_url, "source": (c.meta or {}).get("auto_source")}
                for c in auto
            ],
        }
        report.append(row)
        print(f"\n{sym} {ev_d}: catalog={len(catalog)} auto={len(auto)}")
        for c in catalog:
            print(f"  [catalog] {c.material_type}: {c.source_url}")
        for c in auto:
            print(f"  [auto] {c.material_type}: {c.source_url}")

    if args.register and events:
        import subprocess

        sym_csv = ",".join(sorted({s for s, _ in events}))
        cmd = [
            sys.executable,
            str(project_root / "scripts" / "sync_earnings_material_registry.py"),
            "--ensure-table",
            "--since",
            args.since,
            "--symbols",
            sym_csv,
            "--discover-links",
        ]
        if not fool_rate_limit_active():
            cmd.append("--auto-fool")
        logger.info("run: %s", " ".join(cmd))
        subprocess.call(cmd, cwd=str(project_root))

    if args.json_out.strip():
        Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

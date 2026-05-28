#!/usr/bin/env python3
"""
Run full earnings intelligence pipeline for GAME_5M + portfolio universe.

Steps: sync registry → ingest materials → LLM extract → optional Event Brief.

Examples:
  python scripts/run_earnings_intelligence_pipeline.py --dry-run
  python scripts/run_earnings_intelligence_pipeline.py --since 2026-01-01 --extract-limit 8
  python scripts/run_earnings_intelligence_pipeline.py --symbols MSFT,AMD --brief
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_event_brief import build_event_brief  # noqa: E402
from services.earnings_intelligence_universe import (  # noqa: E402
    get_earnings_intelligence_universe,
    universe_symbols_csv,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def _run_py(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(project_root / script), *args]
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd)


def _recent_extracted_events(engine, *, since: date | None, symbols: set[str] | None, limit: int) -> list[tuple[str, date]]:
    from sqlalchemy import text

    where = ["ed.knowledge_base_id IS NOT NULL"]
    params: dict = {"limit": limit}
    if since:
        where.append("kb.ts::date >= :since")
        params["since"] = since
    if symbols:
        where.append("UPPER(TRIM(kb.ticker)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    q = text(
        f"""
        SELECT UPPER(TRIM(kb.ticker)) AS symbol, kb.ts::date AS event_date
        FROM earnings_event_detail ed
        JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
        WHERE {' AND '.join(where)}
        ORDER BY kb.ts DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).all()
    out: list[tuple[str, date]] = []
    for sym, ev_d in rows:
        if isinstance(ev_d, date):
            out.append((str(sym).upper(), ev_d))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings intelligence pipeline (universe-wide)")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--symbols", default="", help="Override universe with comma-separated tickers")
    ap.add_argument("--sync-limit", type=int, default=500)
    ap.add_argument("--ingest-limit", type=int, default=40)
    ap.add_argument("--extract-limit", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="Token plan only for extract step")
    ap.add_argument("--skip-sync", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--skip-extract", action="store_true")
    ap.add_argument("--no-auto-fool", action="store_true", help="Skip Fool URL probing on sync (faster cron)")
    ap.add_argument("--brief", action="store_true", help="Build Event Brief for recently extracted events")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    sym_csv = args.symbols.strip() or universe_symbols_csv()
    sym_set = {s.strip().upper() for s in sym_csv.split(",") if s.strip()}
    logger.info("Pipeline universe: %s tickers", len(sym_set))

    sync_args = [
        "--since",
        args.since,
        "--symbols",
        sym_csv,
        "--limit",
        str(args.sync_limit),
        "--discover-links",
    ]
    if args.no_auto_fool:
        sync_args.append("--no-auto-fool")

    if not args.skip_sync:
        rc = _run_py("scripts/sync_earnings_material_registry.py", sync_args)
        if rc != 0:
            return rc

    if not args.skip_ingest:
        rc = _run_py(
            "scripts/ingest_earnings_materials.py",
            [
                "--status",
                "registered,failed,downloaded",
                "--limit",
                str(args.ingest_limit),
            ],
        )
        if rc != 0:
            return rc

    extract_args = [
        "--since",
        args.since,
        "--symbols",
        sym_csv,
        "--limit",
        str(args.extract_limit),
    ]
    if args.dry_run:
        extract_args.append("--dry-run")
    if not args.skip_extract:
        rc = _run_py("scripts/extract_earnings_material_facts.py", extract_args)
        if rc != 0 and not args.dry_run:
            return rc

    results: dict = {"universe_size": len(sym_set), "universe": sorted(sym_set)}
    if args.brief:
        engine = get_engine()
        briefs = []
        for sym, ev_d in _recent_extracted_events(
            engine,
            since=_parse_date(args.since),
            symbols=sym_set,
            limit=max(1, args.extract_limit),
        ):
            brief = build_event_brief(engine, symbol=sym, event_date=ev_d)
            briefs.append(brief)
            logger.info("brief %s %s status=%s", sym, ev_d, brief.get("status"))
        results["briefs"] = briefs

    out_path = args.json_out.strip()
    if not out_path:
        out_dir = project_root / "logs" / "earnings_materials"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "pipeline_run.json")
    Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

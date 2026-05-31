#!/usr/bin/env python3
"""
Cron entry: полный цикл earnings materials без ручных шагов.

KB calendar (yfinance) → sync registry → скачивание документов → LLM extract → readiness JSON.

ML train / shadow остаются в run_earnings_ml_refresh.py (каждые 6h + nightly --full).
Analyzer смотрит last_earnings_intelligence_readiness.json и report_daily.json.

Examples:
  python scripts/run_earnings_intelligence_autoprep.py
  python scripts/run_earnings_intelligence_autoprep.py --dry-run
  python scripts/run_earnings_intelligence_autoprep.py --skip-kb-yfinance
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    logger.info("run: %s", " ".join(cmd))
    full_env = None
    if env:
        import os

        full_env = os.environ.copy()
        full_env.update(env)
    return subprocess.call(cmd, cwd=str(project_root), env=full_env)


def _q_dir() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    return project_root / "local" / "logs" / "ml_data_quality"


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings intelligence autoprep (materials full cycle)")
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--symbols", default="", help="Override universe CSV")
    ap.add_argument("--sync-limit", type=int, default=500)
    ap.add_argument("--ingest-limit", type=int, default=40)
    ap.add_argument("--extract-limit", type=int, default=12)
    ap.add_argument("--kb-earnings-limit", type=int, default=8, help="Max new KB rows per yfinance pass")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-kb-yfinance", action="store_true")
    ap.add_argument("--skip-readiness", action="store_true")
    ap.add_argument("--no-auto-fool", action="store_true", default=True, help="Skip Fool probing (default for cron)")
    ap.add_argument("--auto-fool", action="store_false", dest="no_auto_fool")
    args = ap.parse_args()

    from services.earnings_intelligence_universe import universe_symbols_csv

    py = sys.executable
    sym_csv = args.symbols.strip() or universe_symbols_csv()
    sym_set = {s.strip().upper() for s in sym_csv.split(",") if s.strip()}
    logger.info("Autoprep universe: %s tickers", len(sym_set))

    steps: dict[str, int] = {}

    if not args.skip_kb_yfinance:
        yf_cmd = [
            py,
            "scripts/ingest_earnings_event_details_yfinance.py",
            "--tickers",
            sym_csv,
            "--ensure-kb-events",
            "--earnings-limit",
            str(max(1, args.kb_earnings_limit)),
        ]
        if args.dry_run:
            yf_cmd.append("--dry-run")
        rc = _run(yf_cmd)
        steps["kb_yfinance_seed"] = rc
        if rc != 0 and not args.dry_run:
            logger.warning("kb_yfinance_seed exited %s — continuing sync/ingest", rc)

    sync_cmd = [
        py,
        "scripts/sync_earnings_material_registry.py",
        "--ensure-table",
        "--since",
        args.since,
        "--symbols",
        sym_csv,
        "--limit",
        str(max(1, args.sync_limit)),
        "--discover-links",
    ]
    if args.no_auto_fool:
        sync_cmd.append("--no-auto-fool")
    if args.dry_run:
        sync_cmd.append("--dry-run")
    rc = _run(sync_cmd)
    steps["materials_sync"] = rc
    if rc != 0 and not args.dry_run:
        return rc

    ingest_cmd = [
        py,
        "scripts/ingest_earnings_materials.py",
        "--ensure-table",
        "--status",
        "registered,failed,downloaded",
        "--limit",
        str(max(1, args.ingest_limit)),
    ]
    if args.dry_run:
        ingest_cmd.append("--dry-run")
    rc = _run(ingest_cmd)
    steps["materials_ingest"] = rc
    if rc != 0 and not args.dry_run:
        logger.warning("materials_ingest exited %s — continuing extract", rc)

    extract_cmd = [
        py,
        "scripts/extract_earnings_material_facts.py",
        "--since",
        args.since,
        "--symbols",
        sym_csv,
        "--limit",
        str(max(1, args.extract_limit)),
    ]
    if args.dry_run:
        extract_cmd.append("--dry-run")
    rc = _run(extract_cmd)
    steps["materials_extract"] = rc
    if rc != 0 and not args.dry_run:
        logger.warning("materials_extract exited %s", rc)

    readiness_summary: dict = {}
    if not args.skip_readiness and not args.dry_run:
        from report_generator import get_engine
        from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness

        bundle = write_earnings_intelligence_readiness(get_engine(), project_root=project_root)
        gates = bundle.get("gates") or {}
        readiness_summary = {
            "overall_grid_ready": gates.get("overall_grid_ready"),
            "overall_peer_spillover_ready": gates.get("overall_peer_spillover_ready"),
            "overall_earnings_autoprep_ready": gates.get("overall_earnings_autoprep_ready"),
            "overall_open_path_mvp_prerequisites_ready": gates.get("overall_open_path_mvp_prerequisites_ready"),
            "sources_ready": (gates.get("sources") or {}).get("ready"),
            "features_ready": (gates.get("features") or {}).get("ready"),
            "scenario_labels_ready": (gates.get("scenario_labels") or {}).get("ready"),
        }
        steps["readiness_snapshot"] = 0

    summary = {
        "script": "run_earnings_intelligence_autoprep",
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "universe_size": len(sym_set),
        "steps": steps,
        "readiness": readiness_summary,
        "next_cron": "run_earnings_ml_refresh.py каждые 6h + nightly --full (labels/train/spillover/shadow)",
    }
    out_dir = _q_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "last_earnings_intelligence_autoprep.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("Autoprep summary → %s gates=%s", out_path, readiness_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Prod evaluation: materials → labels → ML refresh → shadow → readiness summary.

Default ticker scope: full earnings intelligence universe (GAME_5M + portfolio + spillover),
not a hardcoded subset. Use --symbols only for targeted debug runs.

Examples:
  python scripts/run_earnings_intelligence_prod_eval.py --dry-run
  python scripts/run_earnings_intelligence_prod_eval.py --symbols DELL --skip-materials
  ML_READINESS_TRAIN_MODE=full python scripts/run_earnings_intelligence_prod_eval.py
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _run(cmd: list[str]) -> int:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(project_root))


def main() -> int:
    ap = argparse.ArgumentParser(description="Earnings intelligence prod eval (5 steps)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--symbols",
        default="",
        help="Override universe with comma-separated tickers (default: earnings intelligence universe)",
    )
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--ingest-limit", type=int, default=40)
    ap.add_argument("--extract-limit", type=int, default=50)
    ap.add_argument("--skip-materials", action="store_true")
    ap.add_argument("--skip-ml-refresh", action="store_true")
    ap.add_argument("--skip-shadow", action="store_true")
    args = ap.parse_args()

    from config_loader import get_config_value
    from services.earnings_intelligence_universe import universe_symbols_csv

    py = sys.executable
    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full = mode in ("full", "train", "write", "prod") and not args.dry_run
    sym_arg = args.symbols.strip() or universe_symbols_csv()
    sym_set = {s.strip().upper() for s in sym_arg.split(",") if s.strip()}
    logger.info("Prod eval universe: %s tickers (%s)", len(sym_set), sorted(sym_set))

    steps: list[tuple[str, int]] = []

    if not args.skip_materials:
        yf_cmd = [
            py,
            "scripts/ingest_earnings_event_details_yfinance.py",
            "--tickers",
            sym_arg,
            "--ensure-kb-events",
            "--earnings-limit",
            "6",
        ]
        if args.dry_run:
            yf_cmd.append("--dry-run")
        rc = _run(yf_cmd)
        steps.append(("kb_yfinance_seed", rc))
        if rc != 0 and not args.dry_run:
            return rc

        sync_cmd = [
            py,
            "scripts/sync_earnings_material_registry.py",
            "--ensure-table",
            "--since",
            args.since,
            "--symbols",
            sym_arg,
            "--discover-links",
        ]
        if args.dry_run:
            sync_cmd.append("--dry-run")
        rc = _run(sync_cmd)
        steps.append(("materials_sync", rc))
        if rc != 0 and not args.dry_run:
            return rc

        ingest_cmd = [
            py,
            "scripts/ingest_earnings_materials.py",
            "--ensure-table",
            "--limit",
            str(max(1, args.ingest_limit)),
        ]
        if args.dry_run:
            ingest_cmd.append("--dry-run")
        rc = _run(ingest_cmd)
        steps.append(("materials_ingest", rc))

        extract_cmd = [
            py,
            "scripts/extract_earnings_material_facts.py",
            "--symbols",
            sym_arg,
            "--limit",
            str(max(1, args.extract_limit)),
        ]
        if args.dry_run:
            extract_cmd.append("--dry-run")
        rc = _run(extract_cmd)
        steps.append(("materials_extract", rc))

    if not args.skip_ml_refresh:
        refresh_cmd = [py, "scripts/run_earnings_ml_refresh.py"]
        if args.dry_run:
            refresh_cmd.append("--dry-run")
        rc = _run(refresh_cmd)
        steps.append(("ml_refresh", rc))
        if rc != 0 and not args.dry_run:
            return rc

    if not args.skip_shadow:
        shadow_cmd = [py, "scripts/run_earnings_scenario_shadow_report.py", "--since", args.since]
        rc = _run(shadow_cmd)
        steps.append(("shadow_report", rc))

    from report_generator import get_engine
    from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness

    readiness = write_earnings_intelligence_readiness(get_engine(), project_root=project_root)
    steps.append(("readiness", 0))

    summary = {
        "eval_version": "earnings_intelligence_prod_eval_v1",
        "dry_run": args.dry_run or not full,
        "universe_size": len(sym_set),
        "universe": sorted(sym_set),
        "steps": {name: rc for name, rc in steps},
        "overall_grid_ready": (readiness.get("gates") or {}).get("overall_grid_ready"),
        "trading_shadow_ready": (readiness.get("gates") or {}).get("trading_shadow", {}).get("ready"),
        "fusion_advisory_only": True,
    }
    out_dir = Path("/app/logs/ml/ml_data_quality") if Path("/app/logs").exists() else project_root / "local/logs/ml_data_quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "last_earnings_intelligence_prod_eval.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Prod eval summary → %s %s", out_path, json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

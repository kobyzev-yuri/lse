#!/usr/bin/env python3
"""Send Earnings autoprep ops digest to Telegram (manual or refresh)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _autoprep_summary_path(project_root: Path) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_earnings_intelligence_autoprep.json")
    return project_root / "local" / "logs" / "ml_data_quality" / "last_earnings_intelligence_autoprep.json"


def _refresh_pending(summary: dict) -> dict:
    from report_generator import get_engine
    from services.earnings_calendar_new_events import load_materials_pipeline_calendar_events

    since_raw = (summary.get("since") or "2026-01-01")[:10]
    since_d = date.fromisoformat(since_raw)
    summary = dict(summary)
    summary["pending_calendar_events"] = len(
        load_materials_pipeline_calendar_events(get_engine(), since=since_d, limit=100)
    )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram earnings autoprep digest")
    ap.add_argument("--force", action="store_true", help="Send even if already sent today (UTC)")
    ap.add_argument("--refresh-pending", action="store_true", default=True)
    ap.add_argument("--no-refresh-pending", action="store_false", dest="refresh_pending")
    args = ap.parse_args()

    from services.earnings_autoprep_digest import maybe_send_autoprep_daily_digest

    path = _autoprep_summary_path(project_root)
    if path.is_file():
        summary = json.loads(path.read_text(encoding="utf-8"))
    else:
        summary = {"steps": {}, "readiness": {}}

    if args.refresh_pending:
        summary = _refresh_pending(summary)

    if not summary.get("readiness"):
        from services.earnings_intelligence_readiness import write_earnings_intelligence_readiness

        bundle = write_earnings_intelligence_readiness(
            __import__("report_generator").get_engine(),
            project_root=project_root,
        )
        gates = bundle.get("gates") or {}
        summary["readiness"] = {
            "overall_grid_ready": gates.get("overall_grid_ready"),
            "overall_peer_spillover_ready": gates.get("overall_peer_spillover_ready"),
            "overall_earnings_autoprep_ready": gates.get("overall_earnings_autoprep_ready"),
            "earnings_autoprep": gates.get("earnings_autoprep") or {},
        }

    sent = maybe_send_autoprep_daily_digest(summary, project_root=project_root, force=args.force)
    if not sent:
        from services.earnings_autoprep_digest import format_autoprep_digest_message

        print(format_autoprep_digest_message(summary))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

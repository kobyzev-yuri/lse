#!/usr/bin/env python3
"""Scheduled GAME_5M tuning cycle: replay propose + optional observe.

Cron-friendly entrypoint. Does not auto-apply config changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.game5m_replay_proposals import build_game5m_replay_proposals  # noqa: E402
from services.game5m_tuning_ledger import closed_summary, load_ledger, save_ledger  # noqa: E402

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_propose(args: argparse.Namespace) -> int:
    logging.getLogger("services.game_5m").setLevel(logging.WARNING)
    engine = get_engine()
    report = build_game5m_replay_proposals(
        engine,
        days=args.days,
        max_trades=args.max_trades,
        exchange=args.exchange,
        horizon_tail_days=args.horizon_tail_days,
        include_false_takes=args.include_false_takes,
        top_n=args.top_n,
        families=args.families,
        max_ticker_candidates=args.max_ticker_candidates,
    )
    ledger = load_ledger(args.ledger)
    ledger["latest_proposals"] = report
    ledger.setdefault("history", []).append({"type": "propose", "at_utc": _utc_now(), "params": report.get("params")})
    path = save_ledger(ledger, args.ledger)
    top = (report.get("proposals") or [])[:3]
    logger.info(
        "propose done ledger=%s trades=%s candidates=%s top=%s",
        path,
        (report.get("selection") or {}).get("closed_trades_selected"),
        report.get("candidate_count"),
        [(p.get("env_key"), p.get("proposed"), p.get("score")) for p in top],
    )
    print(json.dumps({"ok": True, "ledger": str(path), **report}, ensure_ascii=False, indent=2))
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    ledger = load_ledger(args.ledger)
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if not active:
        print(json.dumps({"ok": True, "skipped": "no_active_experiment"}, ensure_ascii=False))
        return 0
    days = max(1, int(args.days or active.get("observe_days") or 2))
    min_new = max(1, int(args.min_new_trades))
    summary = closed_summary(days)
    obs = {"at_utc": _utc_now(), "summary": summary}
    active.setdefault("observations", []).append(obs)
    baseline_total = int((active.get("baseline_summary") or {}).get("closed_trades") or 0)
    if int(summary.get("closed_trades") or 0) >= baseline_total + min_new:
        active["status"] = "ready_for_review"
        active["ready_at_utc"] = _utc_now()
    ledger["active_experiment"] = active
    ledger.setdefault("history", []).append({"type": "observation", "experiment_id": active.get("experiment_id"), **obs})
    path = save_ledger(ledger, args.ledger)
    print(json.dumps({"ok": True, "ledger": str(path), "active_experiment": active}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GAME_5M scheduled tuning cycle")
    parser.add_argument("--ledger", default="", help="Ledger path (default GAME5M_TUNING_LEDGER or local/game5m_tuning_ledger.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="Weekly replay proposals into ledger")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--max-trades", type=int, default=40)
    p.add_argument("--top-n", type=int, default=8)
    p.add_argument("--exchange", default="US")
    p.add_argument("--horizon-tail-days", type=int, default=1)
    p.add_argument("--include-false-takes", action="store_true")
    p.add_argument("--families", default="exit", choices=("exit", "all"), help="exit=global exit keys only (faster)")
    p.add_argument("--max-ticker-candidates", type=int, default=4, help="Per-ticker caps when families=all")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("observe", help="Daily observation for active experiment")
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--min-new-trades", type=int, default=8)
    p.set_defaults(func=cmd_observe)
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

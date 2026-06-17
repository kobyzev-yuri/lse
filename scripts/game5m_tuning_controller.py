#!/usr/bin/env python3
"""Single controller for GAME_5M replay-based parameter tuning.

Default operations are read-only. `apply` changes config.env only after shared
policy validation and only if no tuning experiment is already pending.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.game5m_replay_proposals import build_game5m_replay_proposals  # noqa: E402
from services.game5m_tuning_ledger import closed_summary, find_proposal, ledger_path, load_ledger, save_ledger  # noqa: E402
from services.game5m_tuning_policy import (
    apply_game5m_bundle,
    apply_game5m_update,
    current_config_value,
    rollback_game5m_experiment_applied,
    validate_game5m_bundle,
    validate_game5m_update,
)
from services.game5m_tuning_bundles import get_bundle, list_bundles


DEFAULT_LEDGER = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_display(raw: str = "") -> str:
    return str(ledger_path(raw))


def _load_json(raw: str = "") -> Dict[str, Any]:
    return load_ledger(raw)


def _save_json(raw: str, data: Dict[str, Any]) -> None:
    save_ledger(data, raw)


def _closed_summary(days: int) -> Dict[str, Any]:
    return closed_summary(days)


def _find_proposal(ledger: Dict[str, Any], proposal_id: str) -> Optional[Dict[str, Any]]:
    return find_proposal(ledger, proposal_id)


def cmd_propose(args: argparse.Namespace) -> None:
    if args.quiet_replay_logs:
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
    ledger_raw = (args.ledger or "").strip()
    ledger = _load_json(ledger_raw)
    ledger["latest_proposals"] = report
    ledger.setdefault("history", [])
    ledger["updated_at_utc"] = _utc_now()
    saved = save_ledger(ledger, ledger_raw)
    print(json.dumps({"ok": True, "ledger": str(saved), **report}, ensure_ascii=False, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    ledger_raw = (args.ledger or "").strip()
    ledger = _load_json(ledger_raw)
    latest = ledger.get("latest_proposals") if isinstance(ledger.get("latest_proposals"), dict) else {}
    out = {
        "ok": True,
        "ledger": _ledger_display(ledger_raw),
        "active_experiment": ledger.get("active_experiment"),
        "latest_generated_at_utc": latest.get("generated_at_utc"),
        "latest_proposal_count": len(latest.get("proposals") or []),
        "top_proposals": (latest.get("proposals") or [])[: args.top_n],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_apply(args: argparse.Namespace) -> None:
    ledger_raw = (args.ledger or "").strip()
    ledger = _load_json(ledger_raw)
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if active and active.get("status") == "pending_effect" and not args.force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "active_experiment_pending",
                    "active_experiment": active,
                    "hint": "Use observe/review first, or --force if you intentionally override.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(3)

    proposal = _find_proposal(ledger, args.proposal_id) if args.proposal_id else None
    key = args.key or (proposal or {}).get("env_key")
    value = args.value or (proposal or {}).get("proposed")
    if not key or value is None:
        print(json.dumps({"ok": False, "error": "missing_key_or_value"}, ensure_ascii=False, indent=2))
        sys.exit(2)

    validation = validate_game5m_update(key, value, enforce_step_limits=not args.relaxed)
    if not validation.ok:
        print(json.dumps({"ok": False, "validation": validation.__dict__}, ensure_ascii=False, indent=2))
        sys.exit(4)

    baseline = _closed_summary(args.observe_days)
    ok, record = apply_game5m_update(
        key,
        value,
        source="game5m_tuning_controller",
        dry_run=args.dry_run,
        enforce_step_limits=not args.relaxed,
    )
    experiment = {
        "experiment_id": f"{validation.key}={validation.proposed}@{_utc_now()}",
        "status": "dry_run" if args.dry_run else ("pending_effect" if ok else "failed"),
        "created_at_utc": _utc_now(),
        "proposal_id": (proposal or {}).get("proposal_id"),
        "applied": record,
        "proposal": proposal,
        "baseline_summary": baseline,
        "observe_days": int(args.observe_days),
        "observations": [],
    }
    ledger["active_experiment"] = experiment
    ledger.setdefault("history", []).append(experiment)
    ledger["updated_at_utc"] = _utc_now()
    _save_json(ledger_raw, ledger)
    print(json.dumps({"ok": ok, "ledger": _ledger_display(ledger_raw), "experiment": experiment}, ensure_ascii=False, indent=2))
    if not ok:
        sys.exit(5)


def cmd_apply_bundle(args: argparse.Namespace) -> None:
    ledger_raw = (args.ledger or "").strip()
    ledger = _load_json(ledger_raw)
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if active and active.get("status") == "pending_effect" and not args.force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "active_experiment_pending",
                    "active_experiment": active,
                    "hint": "Use observe/review first, or --force if you intentionally override.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(3)

    bundle_id = str(args.bundle_id or "").strip()
    try:
        bundle = get_bundle(bundle_id)
    except KeyError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(2)

    observe_days = int(args.observe_days or bundle.observe_days_default)
    ok_val, reason, _vals = validate_game5m_bundle(bundle_id, enforce_step_limits=not args.relaxed)
    if not ok_val:
        print(json.dumps({"ok": False, "validation_reason": reason}, ensure_ascii=False, indent=2))
        sys.exit(4)

    baseline = _closed_summary(observe_days)
    ok, applied = apply_game5m_bundle(
        bundle_id,
        source="game5m_tuning_controller",
        dry_run=args.dry_run,
        enforce_step_limits=not args.relaxed,
    )
    experiment = {
        "experiment_id": f"bundle:{bundle_id}@{_utc_now()}",
        "kind": "bundle",
        "bundle_id": bundle_id,
        "status": "dry_run" if args.dry_run else ("pending_effect" if ok else "failed"),
        "created_at_utc": _utc_now(),
        "applied": applied,
        "baseline_summary": baseline,
        "observe_days": observe_days,
        "observations": [],
    }
    ledger["active_experiment"] = experiment
    ledger.setdefault("history", []).append(experiment)
    ledger["updated_at_utc"] = _utc_now()
    _save_json(ledger_raw, ledger)
    print(json.dumps({"ok": ok, "ledger": _ledger_display(ledger_raw), "experiment": experiment}, ensure_ascii=False, indent=2))
    if not ok:
        sys.exit(5)


def cmd_list_bundles(_args: argparse.Namespace) -> None:
    print(json.dumps({"ok": True, "bundles": list_bundles()}, ensure_ascii=False, indent=2))


def cmd_observe(args: argparse.Namespace) -> None:
    ledger_raw = (args.ledger or "").strip()
    ledger = _load_json(ledger_raw)
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if not active:
        print(json.dumps({"ok": False, "error": "no_active_experiment", "ledger": _ledger_display(ledger_raw)}, ensure_ascii=False, indent=2))
        sys.exit(2)
    summary = _closed_summary(args.days or int(active.get("observe_days") or 5))
    obs = {"at_utc": _utc_now(), "summary": summary}
    active.setdefault("observations", []).append(obs)
    baseline_total = int((active.get("baseline_summary") or {}).get("closed_trades") or 0)
    min_new = max(1, int(args.min_new_trades))
    if summary["closed_trades"] >= baseline_total + min_new:
        active["status"] = "ready_for_review"
        active["ready_at_utc"] = _utc_now()
    ledger["active_experiment"] = active
    ledger.setdefault("history", []).append({"type": "observation", "experiment_id": active.get("experiment_id"), **obs})
    ledger["updated_at_utc"] = _utc_now()
    _save_json(ledger_raw, ledger)
    print(json.dumps({"ok": True, "ledger": _ledger_display(ledger_raw), "active_experiment": active}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GAME_5M replay-based tuning controller")
    parser.add_argument("--ledger", default="", help="Ledger path, default local/game5m_tuning_ledger.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="Build ranked replay proposals; read-only")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--max-trades", type=int, default=120)
    p.add_argument("--top-n", type=int, default=12)
    p.add_argument("--exchange", default="US")
    p.add_argument("--horizon-tail-days", type=int, default=1)
    p.add_argument("--include-false-takes", action="store_true")
    p.add_argument("--quiet-replay-logs", action="store_true", default=True)
    p.add_argument("--families", default="all", choices=("exit", "all", "ticker"))
    p.add_argument("--max-ticker-candidates", type=int, default=8)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("status", help="Show ledger status")
    p.add_argument("--top-n", type=int, default=5)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("apply", help="Apply one proposal or explicit key/value")
    p.add_argument("--proposal-id", default="")
    p.add_argument("--key", default="")
    p.add_argument("--value", default="")
    p.add_argument("--observe-days", type=int, default=5)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--relaxed", action="store_true", help="Disable step-size limits, keep editable/key validation")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("apply-bundle", help="Apply predefined multi-parameter bundle")
    p.add_argument("--bundle-id", default="overnight_multiday_v1")
    p.add_argument("--observe-days", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--relaxed", action="store_true", help="Disable step-size limits for bundle keys")
    p.set_defaults(func=cmd_apply_bundle)

    p = sub.add_parser("list-bundles", help="List registered tuning bundles")
    p.set_defaults(func=cmd_list_bundles)

    p = sub.add_parser("observe", help="Attach post-apply observation to active experiment")
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--min-new-trades", type=int, default=8)
    p.set_defaults(func=cmd_observe)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

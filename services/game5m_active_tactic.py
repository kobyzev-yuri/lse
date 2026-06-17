# -*- coding: utf-8 -*-
"""Active GAME_5M tactic experiment (bundle) — snapshot for trade context_json."""
from __future__ import annotations

from typing import Any, Dict, Optional

TACTIC_CONTEXT_KEYS = (
    "active_bundle_id",
    "active_experiment_id",
    "active_tactic_kind",
    "active_experiment_status",
)


def get_active_tactic_snapshot(*, ledger_raw: str = "") -> Dict[str, Any]:
    """Read active experiment from tuning ledger; empty dict if none."""
    from services.game5m_tuning_ledger import load_ledger

    ledger = load_ledger(ledger_raw)
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if not active:
        return {}

    bundle_id = active.get("bundle_id")
    experiment_id = active.get("experiment_id")
    kind = active.get("kind") or ("bundle" if bundle_id else "single_key")
    status = active.get("status")

    out: Dict[str, Any] = {
        "active_experiment_id": str(experiment_id) if experiment_id else None,
        "active_tactic_kind": str(kind) if kind else None,
        "active_experiment_status": str(status) if status else None,
    }
    if bundle_id:
        out["active_bundle_id"] = str(bundle_id)
    return {k: v for k, v in out.items() if v is not None}


def enrich_context_with_active_tactic(
    ctx: Optional[Dict[str, Any]],
    *,
    entry_ctx: Optional[Dict[str, Any]] = None,
    at_exit: bool = False,
) -> Dict[str, Any]:
    """
    Stamp tactic fields on BUY (at entry) or SELL context.
    BUY: current ledger active → active_bundle_id.
    SELL: preserve entry stamps; add active_bundle_id_at_exit if ledger changed.
    """
    out = dict(ctx) if isinstance(ctx, dict) else {}
    if entry_ctx and isinstance(entry_ctx, dict):
        for k in TACTIC_CONTEXT_KEYS:
            if entry_ctx.get(k) is not None:
                out[k] = entry_ctx[k]

    current = get_active_tactic_snapshot()
    if at_exit and current.get("active_bundle_id"):
        out["active_bundle_id_at_exit"] = current["active_bundle_id"]
        if current.get("active_experiment_id"):
            out["active_experiment_id_at_exit"] = current["active_experiment_id"]
    elif not at_exit:
        for k, v in current.items():
            out[k] = v
    elif not out.get("active_bundle_id") and current.get("active_bundle_id"):
        for k, v in current.items():
            out[k] = v
    return out


def tactic_from_context(ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract tactic stamp fields from normalized entry/exit context."""
    if not isinstance(ctx, dict):
        return {}
    return {k: ctx.get(k) for k in TACTIC_CONTEXT_KEYS if ctx.get(k) is not None}

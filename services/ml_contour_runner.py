"""Shared helpers for contour refresh scripts (trigger plan + log finalize)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.engine import Engine

from services.ml_contour_deltas import count_deltas_for_contour, resolve_readiness_gates
from services.ml_contour_refresh import (
    RetrainTrigger,
    evaluate_retrain_trigger,
    get_contour_spec,
    load_refresh_log,
    write_refresh_log,
)


def plan_contour_refresh(
    contour_id: str,
    project_root: Path,
    engine: Engine,
    *,
    force_full: bool = False,
    force_apply: bool = False,
) -> Tuple[RetrainTrigger, Dict[str, bool], Dict[str, Optional[int]]]:
    spec = get_contour_spec(contour_id)
    prev = load_refresh_log(project_root, spec)
    gates = resolve_readiness_gates(project_root, spec, engine)
    deltas = count_deltas_for_contour(engine, spec, prev)
    trigger = evaluate_retrain_trigger(
        spec,
        prev,
        new_units_since_last_apply=deltas.get("new_units_apply"),
        new_units_since_last_train=deltas.get("new_units_train"),
        product_ready=gates.get("product_ready", False),
        dataset_ready=gates.get("dataset_ready", False),
        train_ready=gates.get("train_ready", False),
        force_full=force_full,
        force_apply=force_apply,
    )
    return trigger, gates, deltas


def finalize_contour_refresh(
    project_root: Path,
    contour_id: str,
    trigger: RetrainTrigger,
    *,
    apply_ran: bool,
    train_ran: bool,
    full: bool,
    skipped: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    spec = get_contour_spec(contour_id)
    prev = load_refresh_log(project_root, spec)
    now = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "trigger_reasons": trigger.reasons,
        "phase": trigger.phase,
        "apply_data": apply_ran,
        "train_ran": train_ran,
        "full": full,
        "skipped_no_trigger": skipped,
        "new_units_since_last_apply": trigger.new_units_since_last_apply,
        "new_units_since_last_train": trigger.new_units_since_last_train,
        "staleness_hours_apply": trigger.staleness_hours_apply,
        "staleness_hours_train": trigger.staleness_hours_train,
        "last_apply_at_utc": now if apply_ran else prev.get("last_apply_at_utc"),
        "last_train_at_utc": now if train_ran else prev.get("last_train_at_utc"),
    }
    if extra:
        payload.update(extra)
    return write_refresh_log(project_root, spec, payload)

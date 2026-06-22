"""Hold-bar dataset schema for GAME_5M exit/hold ML bake-off (y_hold_good)."""
from __future__ import annotations

import math
from typing import Any, Literal

from services.game5m_ml_context_features import (
    HOLD_BAR_TRAIN_NUMERIC_KEYS,
    HOLD_ENTRY_SNAPSHOT_KEYS,
    HOLD_EXIT_TECH_KEYS,
    HOLD_STATE_KEYS,
    context_vector_from_dict,
    entry_snapshot_from_context,
    hold_exit_tech_from_features,
    hold_state_features,
)
from services.game5m_triple_barrier import recovery_y_label

HOLD_BAR_ML_SCHEMA_VERSION = "1"

HOLD_BAR_ML_SCHEMA: dict[str, Any] = {
    "version": HOLD_BAR_ML_SCHEMA_VERSION,
    "unit": "(trade_id, bar_ts_et) open GAME_5M long",
    "primary_label": "y_hold_good",
    "label_rule": "recovery forward MFE/MAE over H minutes (game5m_triple_barrier.recovery_y_label)",
    "feature_layers": {
        "state": list(HOLD_STATE_KEYS),
        "entry_snapshot": list(HOLD_ENTRY_SNAPSHOT_KEYS),
        "exit_tech": list(HOLD_EXIT_TECH_KEYS),
        "exit_context": "ENTRY_CONTEXT_NUMERIC_KEYS at hold bar",
    },
    "docs": "docs/GAME_5M_EXIT_HOLD_ML_BAKEOFF_PLAN.md",
}

FeatureMode = Literal["recovery", "full"]


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return default


def resolve_hold_train_numeric_keys(mode: FeatureMode = "full") -> tuple[str, ...]:
    if mode == "recovery":
        return (
            "ref_close",
            "entry_price",
            "pnl_pct",
            "hold_minutes",
            "minutes_after_rth_open",
            "dow",
            "hour_et",
            "entry_rsi_5m",
            "entry_vol_5m_pct",
            "entry_momentum_2h_pct",
        )
    return HOLD_BAR_TRAIN_NUMERIC_KEYS


def get_hold_bar_train_feature_schema(mode: FeatureMode = "full") -> tuple[list[str], list[int]]:
    keys = resolve_hold_train_numeric_keys(mode)
    colnames = ["ticker"] + list(keys)
    if mode == "recovery":
        return colnames + ["entry_decision"], [0, len(colnames)]
    return colnames, [0]


def row_from_hold_bar_dict(
    row: dict[str, Any],
    ticker: str | None = None,
    *,
    mode: FeatureMode = "full",
) -> list[Any]:
    sym = (ticker or row.get("ticker") or "").strip()
    out: list[Any] = [sym]
    for key in resolve_hold_train_numeric_keys(mode):
        out.append(_safe_float(row.get(key)))
    if mode == "recovery":
        ed = row.get("entry_decision")
        out.append((str(ed).strip()[:64] if ed is not None else "") or "—")
    return out


def y_hold_good_from_row(row: dict[str, Any], *, horizon_minutes: int = 120) -> int | None:
    h = int(horizon_minutes)
    y = row.get(f"h{h}_y_recovery")
    if y is None:
        y = row.get("y_hold_good")
    if y is None:
        mfe = row.get(f"h{h}_mfe_fwd_pct")
        mae = row.get(f"h{h}_mae_fwd_pct")
        eps_up = _safe_float(row.get("label_eps_up_pct"), 0.5)
        max_adv = _safe_float(row.get("label_max_adverse_pct"), -3.0)
        if mfe is not None and mae is not None:
            return recovery_y_label(float(mfe), float(mae), eps_up_pct=eps_up, max_adverse_pct=max_adv)
        return None
    try:
        return int(y)
    except (TypeError, ValueError):
        return None


__all__ = [
    "HOLD_BAR_ML_SCHEMA",
    "HOLD_BAR_ML_SCHEMA_VERSION",
    "HOLD_BAR_TRAIN_NUMERIC_KEYS",
    "FeatureMode",
    "entry_snapshot_from_context",
    "get_hold_bar_train_feature_schema",
    "hold_exit_tech_from_features",
    "hold_state_features",
    "resolve_hold_train_numeric_keys",
    "row_from_hold_bar_dict",
    "y_hold_good_from_row",
]

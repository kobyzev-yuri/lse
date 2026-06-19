"""Bar-level entry dataset schema (builder: scripts/build_game5m_entry_bar_dataset.py)."""
from __future__ import annotations

import math
from typing import Any

from services.game5m_triple_barrier import (
    ENTRY_BAR_ML_SCHEMA,
    ENTRY_BAR_ML_SCHEMA_VERSION,
    TripleBarrierConfig,
    TripleBarrierResult,
    triple_barrier_config_from_env,
    triple_barrier_forward,
)

# Offline train v2 (bar dataset) — subset of builder CSV columns; prod v1 unchanged.
BAR_TRAIN_NUMERIC_KEYS: tuple[str, ...] = (
    "rsi_5m",
    "momentum_2h_pct",
    "momentum_rth_today_pct",
    "volatility_5m_pct",
    "pullback_from_high_pct",
    "bars_count",
    "momentum_rth_today_bars",
    "price_to_low5d_ratio",
)


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


def get_bar_train_feature_schema() -> tuple[list[str], list[int]]:
    colnames = ["ticker"] + list(BAR_TRAIN_NUMERIC_KEYS)
    return colnames, [0]


def row_from_bar_dataset_dict(row: dict[str, Any], ticker: str | None = None) -> list[Any]:
    sym = (ticker or row.get("ticker") or "").strip()
    out: list[Any] = [sym]
    for key in BAR_TRAIN_NUMERIC_KEYS:
        out.append(_safe_float(row.get(key)))
    return out


__all__ = [
    "BAR_TRAIN_NUMERIC_KEYS",
    "ENTRY_BAR_ML_SCHEMA",
    "ENTRY_BAR_ML_SCHEMA_VERSION",
    "TripleBarrierConfig",
    "TripleBarrierResult",
    "get_bar_train_feature_schema",
    "row_from_bar_dataset_dict",
    "triple_barrier_config_from_env",
    "triple_barrier_forward",
]

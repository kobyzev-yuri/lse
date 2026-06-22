"""Bar-level entry dataset schema (builder: scripts/build_game5m_entry_bar_dataset.py)."""
from __future__ import annotations

import math
from typing import Any, Literal

from services.game5m_ml_context_features import ENTRY_CONTEXT_NUMERIC_KEYS
from services.game5m_triple_barrier import (
    ENTRY_BAR_ML_SCHEMA,
    ENTRY_BAR_ML_SCHEMA_VERSION,
    TripleBarrierConfig,
    TripleBarrierResult,
    triple_barrier_config_from_env,
    triple_barrier_forward,
)

FeatureMode = Literal["tech", "full"]

# Offline train v2 (bar dataset) — technical subset.
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

# T + news + calendar + macro (Phase 1.5).
BAR_TRAIN_FULL_NUMERIC_KEYS: tuple[str, ...] = BAR_TRAIN_NUMERIC_KEYS + ENTRY_CONTEXT_NUMERIC_KEYS

# Promotion gate for bar v2 (relaxed from 0.55 → 0.545 after prod AUC 0.5495, 2026-06).
ENTRY_BAR_V2_PROMOTION_AUC_MIN_DEFAULT = 0.545


def entry_bar_v2_promotion_auc_min() -> float:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_ENTRY_BAR_V2_PROMOTION_AUC_MIN", "") or "").strip()
    if not raw:
        raw = (get_config_value("ML_READINESS_ENTRY_BAR_V2_AUC_MIN", "") or "").strip()
    if not raw:
        return ENTRY_BAR_V2_PROMOTION_AUC_MIN_DEFAULT
    try:
        return float(raw.replace(",", "."))
    except (TypeError, ValueError):
        return ENTRY_BAR_V2_PROMOTION_AUC_MIN_DEFAULT


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


def resolve_bar_train_numeric_keys(mode: FeatureMode = "full") -> tuple[str, ...]:
    return BAR_TRAIN_NUMERIC_KEYS if mode == "tech" else BAR_TRAIN_FULL_NUMERIC_KEYS


def get_bar_train_feature_schema(mode: FeatureMode = "full") -> tuple[list[str], list[int]]:
    keys = resolve_bar_train_numeric_keys(mode)
    colnames = ["ticker"] + list(keys)
    return colnames, [0]


def row_from_bar_dataset_dict(
    row: dict[str, Any],
    ticker: str | None = None,
    *,
    mode: FeatureMode = "full",
) -> list[Any]:
    sym = (ticker or row.get("ticker") or "").strip()
    out: list[Any] = [sym]
    for key in resolve_bar_train_numeric_keys(mode):
        out.append(_safe_float(row.get(key)))
    return out


__all__ = [
    "BAR_TRAIN_FULL_NUMERIC_KEYS",
    "BAR_TRAIN_NUMERIC_KEYS",
    "ENTRY_BAR_ML_SCHEMA",
    "ENTRY_BAR_ML_SCHEMA_VERSION",
    "ENTRY_CONTEXT_NUMERIC_KEYS",
    "FeatureMode",
    "TripleBarrierConfig",
    "TripleBarrierResult",
    "get_bar_train_feature_schema",
    "entry_bar_v2_promotion_auc_min",
    "ENTRY_BAR_V2_PROMOTION_AUC_MIN_DEFAULT",
    "resolve_bar_train_numeric_keys",
    "row_from_bar_dataset_dict",
    "triple_barrier_config_from_env",
    "triple_barrier_forward",
]

"""Continuation / underprofit dataset schema (builder: scripts/build_game5m_continuation_dataset.py)."""
from __future__ import annotations

import math
from typing import Any

CONTINUATION_ML_SCHEMA_VERSION = "1"

# Label: post-exit MFE from exit price >= min_extra_upside_pct (builder default 1.0%).
CONTINUATION_ML_DEFAULT_MIN_EXTRA_UPSIDE_PCT = 1.0
CONTINUATION_ML_PROMOTION_AUC_MIN_DEFAULT = 0.55
CONTINUATION_ML_MIN_TAKE_ROWS_DEFAULT = 50

CONTINUATION_ML_SCHEMA: dict[str, Any] = {
    "version": CONTINUATION_ML_SCHEMA_VERSION,
    "description": (
        "Одна строка = закрытая сделка GAME_5M по TAKE_PROFIT / TAKE_PROFIT_SUSPEND. "
        "Метка label_missed_upside=1 если post-exit high превышает exit price на min_extra_upside_pct "
        "(post-exit окно lookahead_minutes, default 120). Признаки train — только до/на момент exit."
    ),
    "label_columns": [
        "label_missed_upside — 1 если post_exit_mfe_pct >= min_extra_upside_pct",
        "label_take_was_enough — контр-метка «тейк был достаточен»",
        "label_stretch_candidate — быстрый профитный тейк с последующим continuation",
    ],
    "train_feature_columns": [
        "ticker (cat)",
        "realized_pct",
        "minutes_to_exit",
        "entry_rsi_5m",
        "entry_momentum_2h_pct",
        "entry_volatility_5m_pct",
        "entry_prob_up",
        "entry_take_profit_pct",
        "entry_take_to_volatility",
        "exit_rsi_5m",
        "exit_momentum_2h_pct",
        "exit_volatility_5m_pct",
        "pre_exit_30m_return_pct",
        "pre_exit_30m_mfe_pct",
        "trade_mfe_pct",
        "exit_signal_type (cat)",
    ],
    "leakage_note": "post_exit_* колонки только для разметки, не входят в train schema.",
}

CONTINUATION_TRAIN_NUMERIC_KEYS: tuple[str, ...] = (
    "realized_pct",
    "minutes_to_exit",
    "entry_rsi_5m",
    "entry_momentum_2h_pct",
    "entry_volatility_5m_pct",
    "entry_prob_up",
    "entry_take_profit_pct",
    "entry_take_to_volatility",
    "exit_rsi_5m",
    "exit_momentum_2h_pct",
    "exit_volatility_5m_pct",
    "pre_exit_30m_return_pct",
    "pre_exit_30m_mfe_pct",
    "trade_mfe_pct",
)


def continuation_min_extra_upside_pct() -> float:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_CONTINUATION_MIN_EXTRA_UPSIDE_PCT", "") or "").strip()
    if not raw:
        return CONTINUATION_ML_DEFAULT_MIN_EXTRA_UPSIDE_PCT
    try:
        return float(raw.replace(",", "."))
    except (TypeError, ValueError):
        return CONTINUATION_ML_DEFAULT_MIN_EXTRA_UPSIDE_PCT


def continuation_promotion_auc_min() -> float:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_CONTINUATION_PROMOTION_AUC_MIN", "") or "").strip()
    if not raw:
        raw = (get_config_value("ML_READINESS_CONTINUATION_AUC_MIN", "") or "").strip()
    if not raw:
        return CONTINUATION_ML_PROMOTION_AUC_MIN_DEFAULT
    try:
        return float(raw.replace(",", "."))
    except (TypeError, ValueError):
        return CONTINUATION_ML_PROMOTION_AUC_MIN_DEFAULT


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


def get_continuation_train_feature_schema() -> tuple[list[str], list[int]]:
    colnames = ["ticker", "exit_signal_type"] + list(CONTINUATION_TRAIN_NUMERIC_KEYS)
    return colnames, [0, 1]


def row_from_continuation_dataset_dict(row: dict[str, Any]) -> list[Any] | None:
    ticker = str(row.get("ticker") or "").strip().upper()
    if not ticker:
        return None
    sig = str(row.get("exit_signal_type") or "TAKE_PROFIT").strip().upper() or "TAKE_PROFIT"
    out: list[Any] = [ticker, sig]
    for key in CONTINUATION_TRAIN_NUMERIC_KEYS:
        out.append(_safe_float(row.get(key)))
    return out


def default_continuation_dataset_csv_path() -> str:
    from pathlib import Path

    if Path("/app/logs").exists():
        return "/app/logs/ml/datasets/game5m_continuation_dataset.csv"
    root = Path(__file__).resolve().parents[1]
    return str(root / "local" / "datasets" / "game5m_continuation_dataset.csv")


__all__ = [
    "CONTINUATION_ML_SCHEMA",
    "CONTINUATION_ML_SCHEMA_VERSION",
    "CONTINUATION_ML_DEFAULT_MIN_EXTRA_UPSIDE_PCT",
    "CONTINUATION_ML_PROMOTION_AUC_MIN_DEFAULT",
    "CONTINUATION_ML_MIN_TAKE_ROWS_DEFAULT",
    "CONTINUATION_TRAIN_NUMERIC_KEYS",
    "continuation_min_extra_upside_pct",
    "continuation_promotion_auc_min",
    "default_continuation_dataset_csv_path",
    "get_continuation_train_feature_schema",
    "row_from_continuation_dataset_dict",
]

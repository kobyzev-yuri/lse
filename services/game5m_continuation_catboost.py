"""
CatBoost continuation (phase 2): predict missed post-exit upside at TAKE moment.

Training: scripts/train_game5m_continuation_catboost.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.game5m_continuation_dataset import (
    CONTINUATION_TRAIN_NUMERIC_KEYS,
    get_continuation_train_feature_schema,
    row_from_continuation_dataset_dict,
)


def default_continuation_catboost_model_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/models/game5m_continuation_catboost.cbm")
    return root / "local" / "models" / "game5m_continuation_catboost.cbm"


def continuation_catboost_schema() -> Tuple[List[str], List[int]]:
    return get_continuation_train_feature_schema()


def load_continuation_model_meta(model_path: str | Path | None = None) -> Optional[Dict[str, Any]]:
    mp = Path(model_path) if model_path else default_continuation_catboost_model_path()
    meta_path = mp.with_suffix(".meta.json")
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def row_vector_from_trade_effect(
    *,
    ticker: str,
    exit_signal: str,
    realized_pct: float,
    hold_minutes: float,
    entry_rsi_5m: Optional[float] = None,
    entry_momentum_2h_pct: Optional[float] = None,
    entry_vol_5m_pct: Optional[float] = None,
    entry_prob_up: Optional[float] = None,
    entry_take_profit_pct: Optional[float] = None,
    exit_rsi_5m: Optional[float] = None,
    exit_momentum_2h_pct: Optional[float] = None,
    exit_volatility_5m_pct: Optional[float] = None,
    pre_exit_30m_return_pct: Optional[float] = None,
    pre_exit_30m_mfe_pct: Optional[float] = None,
    trade_mfe_pct: Optional[float] = None,
) -> Optional[List[Any]]:
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    take_pct = entry_take_profit_pct
    entry_vol = entry_vol_5m_pct
    take_to_vol = None
    if take_pct is not None and entry_vol and entry_vol > 0:
        take_to_vol = take_pct / entry_vol
    row = {
        "ticker": sym,
        "exit_signal_type": str(exit_signal or "TAKE_PROFIT").strip().upper() or "TAKE_PROFIT",
        "realized_pct": realized_pct,
        "minutes_to_exit": hold_minutes,
        "entry_rsi_5m": entry_rsi_5m,
        "entry_momentum_2h_pct": entry_momentum_2h_pct,
        "entry_volatility_5m_pct": entry_vol_5m_pct,
        "entry_prob_up": entry_prob_up,
        "entry_take_profit_pct": take_pct,
        "entry_take_to_volatility": take_to_vol,
        "exit_rsi_5m": exit_rsi_5m,
        "exit_momentum_2h_pct": exit_momentum_2h_pct,
        "exit_volatility_5m_pct": exit_volatility_5m_pct,
        "pre_exit_30m_return_pct": pre_exit_30m_return_pct,
        "pre_exit_30m_mfe_pct": pre_exit_30m_mfe_pct,
        "trade_mfe_pct": trade_mfe_pct,
    }
    return row_from_continuation_dataset_dict(row)


def predict_continuation_missed_upside_proba(
    model_path: str | Path,
    row: Sequence[Any],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    mp = Path(model_path)
    if not mp.is_file():
        return {"status": "no_model", "reason": f"missing {mp}"}
    meta = meta if isinstance(meta, dict) else load_continuation_model_meta(mp)
    try:
        from catboost import CatBoostClassifier
    except ImportError:
        return {"status": "no_catboost", "reason": "catboost not installed"}

    try:
        model = CatBoostClassifier()
        model.load_model(str(mp))
        proba = float(model.predict_proba(list(row))[0][1])
        if not math.isfinite(proba):
            return {"status": "bad_proba", "reason": "non-finite prediction"}
        return {
            "status": "ok",
            "continuation_proba": round(proba, 6),
            "label": "label_missed_upside",
            "meta_trained_at": (meta or {}).get("trained_at"),
        }
    except Exception as e:
        return {"status": "predict_error", "reason": str(e)}


__all__ = [
    "CONTINUATION_TRAIN_NUMERIC_KEYS",
    "continuation_catboost_schema",
    "default_continuation_catboost_model_path",
    "load_continuation_model_meta",
    "predict_continuation_missed_upside_proba",
    "row_from_continuation_dataset_dict",
    "row_vector_from_trade_effect",
]

"""
CatBoost recovery (фаза D): признаки совпадают с JSONL экспортом анализатора (фаза C).

Не в hot path. Обучение: scripts/train_game5m_recovery_catboost.py.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

# Порядок колонок в Pool — должен совпадать с train_game5m_recovery_catboost.py и meta.json.
RECOVERY_CB_FEATURE_NAMES: List[str] = [
    "ticker",
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
    "entry_decision",
]

# Категориальные: ticker (0), entry_decision (11). Не [0,10] — индекс 10 это entry_momentum_2h_pct (float).
RECOVERY_CB_CAT_FEATURE_INDICES: List[int] = [0, 11]


def default_recovery_catboost_model_path() -> Path:
    """
    Если GAME_5M_RECOVERY_CATBOOST_MODEL_PATH не задан — тот же дефолт, что в train_game5m_recovery_catboost.py
    (/app/logs/ml/models/… в контейнере, иначе repo local/models).
    """
    root = Path(__file__).resolve().parents[1]
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/models/game5m_recovery_catboost.cbm")
    return root / "local" / "models" / "game5m_recovery_catboost.cbm"


def recovery_catboost_schema() -> Tuple[List[str], List[int]]:
    return list(RECOVERY_CB_FEATURE_NAMES), list(RECOVERY_CB_CAT_FEATURE_INDICES)


def _minutes_after_rth_open_et(ts_et: pd.Timestamp) -> Optional[float]:
    try:
        day = ts_et.normalize()
        open_et = day + pd.Timedelta(hours=9, minutes=30)
        return float((ts_et - open_et) / pd.Timedelta(minutes=1))
    except Exception:
        return None


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def row_vector_from_export_record(rec: Dict[str, Any]) -> Optional[List[Any]]:
    """Строка признаков из одной записи JSONL экспорта recovery. Без утечек (без trade_id, exit_signal, меток)."""
    if not isinstance(rec, dict):
        return None
    ticker = str(rec.get("ticker") or "").strip().upper()
    if not ticker:
        return None
    entry_price = _num(rec.get("entry_price"), 0.0)
    ref_close = _num(rec.get("ref_close"), 0.0)
    if entry_price <= 0 or ref_close <= 0:
        return None
    mar = rec.get("minutes_after_rth_open")
    if mar is None:
        mar_n = -1.0
    else:
        mar_n = _num(mar, -1.0)
    ed = rec.get("entry_decision")
    ed_s = (str(ed).strip()[:64] if ed is not None else "") or "—"
    return [
        ticker,
        _num(rec.get("ref_close"), 0.0),
        entry_price,
        _num(rec.get("pnl_pct"), 0.0),
        _num(rec.get("hold_minutes"), 0.0),
        mar_n,
        int(rec.get("dow") or 0),
        int(rec.get("hour_et") or 0),
        _num(rec.get("entry_rsi_5m"), 0.0),
        _num(rec.get("entry_vol_5m_pct"), 0.0),
        _num(rec.get("entry_momentum_2h_pct"), 0.0),
        ed_s,
    ]


def row_vector_from_hold_bar(
    *,
    ticker: str,
    entry_price: float,
    entry_ts_et: pd.Timestamp,
    bar_time_et: pd.Timestamp,
    ref_close: float,
    entry_rsi_5m: Optional[float],
    entry_vol_5m_pct: Optional[float],
    entry_momentum_2h_pct: Optional[float],
    entry_decision: Optional[str],
) -> Optional[List[Any]]:
    if entry_price <= 0 or ref_close <= 0:
        return None
    tkr = str(ticker or "").strip().upper()
    if not tkr:
        return None
    pnl_pct = (ref_close / entry_price - 1.0) * 100.0
    hold_min = float((bar_time_et - entry_ts_et) / pd.Timedelta(minutes=1))
    mar = _minutes_after_rth_open_et(bar_time_et)
    mar_n = -1.0 if mar is None else float(mar)
    ed = (str(entry_decision or "").strip()[:64] if entry_decision else "") or "—"
    return [
        tkr,
        float(ref_close),
        float(entry_price),
        round(pnl_pct, 4),
        round(hold_min, 2),
        mar_n,
        int(bar_time_et.dayofweek),
        int(bar_time_et.hour),
        _num(entry_rsi_5m, 0.0),
        _num(entry_vol_5m_pct, 0.0),
        _num(entry_momentum_2h_pct, 0.0),
        ed,
    ]


def predict_recovery_hold_proba(
    model_path: str,
    row: Sequence[Any],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Один скор P(y_recovery=1) для строки признаков (как при обучении).
    """
    out: Dict[str, Any] = {"status": "error", "recovery_proba": None}
    path = Path(model_path)
    if not path.is_file():
        out["status"] = "no_model_file"
        out["reason"] = str(path)
        return out
    try:
        from catboost import CatBoostClassifier, Pool
    except ImportError:
        out["status"] = "no_catboost"
        return out

    names, cats = recovery_catboost_schema()
    if meta and isinstance(meta.get("feature_names"), list):
        names_m = [str(x) for x in meta["feature_names"]]
        if names_m != names:
            out["status"] = "feature_mismatch"
            out["reason"] = f"meta expects {len(names_m)} features, code has {len(names)}"
            return out
    if len(row) != len(names):
        out["status"] = "feature_mismatch"
        out["reason"] = f"row len {len(row)} != {len(names)}"
        return out

    try:
        model = CatBoostClassifier()
        model.load_model(str(path))
        pool = Pool([list(row)], cat_features=cats, feature_names=names)
        proba = model.predict_proba(pool)[0, 1]
        out["status"] = "ok"
        out["recovery_proba"] = float(proba)
    except Exception as exc:
        out["status"] = "predict_error"
        out["reason"] = str(exc)
    return out


def load_recovery_model_meta(model_path: str) -> Optional[Dict[str, Any]]:
    p = Path(model_path).with_suffix(".meta.json")
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None

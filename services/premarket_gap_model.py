# -*- coding: utf-8 -*-
"""Pooled premarket -> RTH open-gap baseline.

The current ticker OLS is fitted independently per ticker. This module adds a
small pooled ridge model trained from the daily gap log so sparse tickers can
borrow strength from the whole universe while still falling back safely.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

MODEL_VERSION = "pooled_ridge_v1"
BASE_FEATURES = (
    "premarket_gap_pct",
    "pred_sector_gap_pct",
    "premarket_return_pct",
    "premarket_range_pct",
    "gap_vs_daily_volatility",
    "abs_premarket_gap_pct",
    "abs_pred_sector_gap_pct",
    "premarket_minus_sector_gap_pct",
)


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _cfg_int(key: str, default: int) -> int:
    try:
        from config_loader import get_config_value

        return int((get_config_value(key, str(default)) or str(default)).strip())
    except Exception:
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        from config_loader import get_config_value

        return float((get_config_value(key, str(default)) or str(default)).strip())
    except Exception:
        return default


def premarket_gap_model_path() -> Path:
    try:
        from config_loader import get_config_value

        raw = (get_config_value("GAME_5M_PREMARKET_GAP_MODEL_PATH", "") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        app_ml = Path("/app/logs/ml/models/premarket_gap")
        try:
            raw = str(app_ml / "pooled_gap_model.json") if app_ml.parent.is_dir() else "local/premarket_gap_model.json"
        except OSError:
            raw = "local/premarket_gap_model.json"
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    return p


def _feature_vector(
    row: Dict[str, Any],
    *,
    symbols: Sequence[str] = (),
) -> Optional[np.ndarray]:
    pm = _to_float(row.get("premarket_gap_pct"))
    sec = _to_float(row.get("pred_sector_gap_pct"))
    if pm is None and sec is None:
        return None
    pm_v = pm if pm is not None else 0.0
    sec_v = sec if sec is not None else 0.0
    pm_ret = _to_float(row.get("premarket_return_pct")) or 0.0
    pm_range = _to_float(row.get("premarket_range_pct")) or 0.0
    gap_vs_vol = _to_float(row.get("gap_vs_daily_volatility")) or 0.0
    vals: List[float] = [
        pm_v,
        sec_v,
        pm_ret,
        pm_range,
        gap_vs_vol,
        abs(pm_v),
        abs(sec_v),
        pm_v - sec_v,
    ]
    sym = str(row.get("symbol") or "").strip().upper()
    vals.extend(1.0 if sym == s else 0.0 for s in symbols)
    return np.array(vals, dtype=float)


def rows_to_training_matrix(
    rows: Iterable[Dict[str, Any]],
    *,
    min_symbol_rows: int = 3,
) -> Tuple[np.ndarray, np.ndarray, List[str], List[Dict[str, Any]]]:
    clean: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for r in rows:
        y = _to_float(r.get("open_gap_pct"))
        if y is None:
            continue
        if _feature_vector(r) is None:
            continue
        rr = dict(r)
        rr["_y"] = y
        clean.append(rr)
        sym = str(r.get("symbol") or "").strip().upper()
        if sym:
            counts[sym] = counts.get(sym, 0) + 1
    symbols = sorted([s for s, n in counts.items() if n >= max(1, int(min_symbol_rows))])
    X_rows: List[np.ndarray] = []
    y_rows: List[float] = []
    used: List[Dict[str, Any]] = []
    for r in clean:
        x = _feature_vector(r, symbols=symbols)
        if x is None:
            continue
        X_rows.append(x)
        y_rows.append(float(r["_y"]))
        used.append(r)
    if not X_rows:
        return np.empty((0, len(BASE_FEATURES))), np.empty((0,)), symbols, []
    return np.vstack(X_rows), np.array(y_rows, dtype=float), symbols, used


def _ridge_fit(X: np.ndarray, y: np.ndarray, l2: float) -> np.ndarray:
    Xb = np.column_stack([np.ones(X.shape[0]), X])
    reg = max(float(l2), 1e-8) * np.eye(Xb.shape[1], dtype=float)
    reg[0, 0] = 0.0
    return np.linalg.solve(Xb.T @ Xb + reg, Xb.T @ y)


def _predict_matrix(X: np.ndarray, weights: Sequence[float]) -> np.ndarray:
    w = np.asarray(weights, dtype=float)
    Xb = np.column_stack([np.ones(X.shape[0]), X])
    return Xb @ w


def _metrics(y: np.ndarray, pred: np.ndarray) -> Dict[str, Any]:
    if len(y) == 0:
        return {"n": 0}
    err = y - pred
    sign = float(np.mean((pred >= 0) == (y >= 0))) if len(y) else None
    return {
        "n": int(len(y)),
        "mae_pp": round(float(np.mean(np.abs(err))), 4),
        "rmse_pp": round(float(np.sqrt(np.mean(err * err))), 4),
        "bias_pp": round(float(np.mean(err)), 4),
        "sign_agreement_rate": round(sign, 4) if sign is not None else None,
    }


def fit_pooled_gap_model(
    rows: Sequence[Dict[str, Any]],
    *,
    min_rows: Optional[int] = None,
    l2: Optional[float] = None,
) -> Dict[str, Any]:
    min_rows = int(min_rows if min_rows is not None else _cfg_int("GAME_5M_PREMARKET_GAP_MODEL_MIN_ROWS", 60))
    l2 = float(l2 if l2 is not None else _cfg_float("GAME_5M_PREMARKET_GAP_MODEL_L2", 5.0))
    X, y, symbols, used = rows_to_training_matrix(rows)
    if len(y) < min_rows:
        return {
            "model_version": MODEL_VERSION,
            "ready": False,
            "reason": f"insufficient_rows:{len(y)}<{min_rows}",
            "n_train": int(len(y)),
        }
    weights = _ridge_fit(X, y, l2)
    pred = _predict_matrix(X, weights)
    residuals = y - pred
    feature_names = ["intercept", *BASE_FEATURES, *[f"symbol:{s}" for s in symbols]]
    return {
        "model_version": MODEL_VERSION,
        "ready": True,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_train": int(len(y)),
        "symbols": symbols,
        "feature_names": feature_names,
        "weights": [round(float(x), 8) for x in weights],
        "l2": l2,
        "metrics_train": _metrics(y, pred),
        "residual_abs_p50_pp": round(float(np.quantile(np.abs(residuals), 0.50)), 4),
        "residual_abs_p80_pp": round(float(np.quantile(np.abs(residuals), 0.80)), 4),
        "residual_abs_p90_pp": round(float(np.quantile(np.abs(residuals), 0.90)), 4),
        "train_window": {
            "first_trade_date": str(min((r.get("trade_date") for r in used), default="")),
            "last_trade_date": str(max((r.get("trade_date") for r in used), default="")),
        },
    }


def evaluate_pooled_gap_model_from_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    min_train_rows: int = 20,
    l2: float = 5.0,
) -> Dict[str, Any]:
    X, y, symbols, _used = rows_to_training_matrix(rows)
    n = len(y)
    if n < min_train_rows + 5:
        return {"mode": "insufficient_data", "n": int(n), "min_train_rows": int(min_train_rows)}
    split = max(min_train_rows, int(n * 0.75))
    if split >= n:
        split = n - 1
    weights = _ridge_fit(X[:split], y[:split], l2)
    pred = _predict_matrix(X[split:], weights)
    return {
        "mode": "ok",
        "model_version": MODEL_VERSION,
        "n_train": int(split),
        "n_eval": int(n - split),
        "n_symbols": len(symbols),
        "eval": _metrics(y[split:], pred),
    }


def load_pooled_gap_artifact(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or premarket_gap_model_path()
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_pooled_gap_artifact(artifact: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = path or premarket_gap_model_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def predict_from_artifact(
    artifact: Dict[str, Any],
    *,
    symbol: str,
    premarket_gap_pct: Optional[float],
    pred_sector_gap_pct: Optional[float],
) -> Tuple[Optional[float], Dict[str, Any]]:
    if not artifact or not artifact.get("ready"):
        return None, {"reason": "artifact_not_ready"}
    weights = artifact.get("weights")
    symbols = artifact.get("symbols") or []
    if not isinstance(weights, list):
        return None, {"reason": "missing_weights"}
    row = {
        "symbol": symbol,
        "premarket_gap_pct": premarket_gap_pct,
        "pred_sector_gap_pct": pred_sector_gap_pct,
    }
    x = _feature_vector(row, symbols=[str(s).upper() for s in symbols])
    if x is None:
        return None, {"reason": "missing_features"}
    pred = float(_predict_matrix(np.vstack([x]), weights)[0])
    mae = _to_float((artifact.get("metrics_train") or {}).get("mae_pp"))
    p80 = _to_float(artifact.get("residual_abs_p80_pp"))
    confidence = None
    if mae is not None:
        confidence = round(max(0.0, min(1.0, 1.0 - mae / 4.0)), 3)
    return round(pred, 3), {
        "model_version": artifact.get("model_version") or MODEL_VERSION,
        "confidence": confidence,
        "uncertainty_p80_pp": p80,
        "n_train": artifact.get("n_train"),
    }


def fetch_gap_training_rows(engine: Any, *, days: int = 180) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    sql = """
        SELECT g.trade_date, g.symbol, g.premarket_gap_pct, g.pred_sector_gap_pct,
               p.premarket_return_pct, p.premarket_range_pct, p.gap_vs_daily_volatility,
               g.macro_risk_level, g.macro_equity_gap_bias, g.open_gap_pct
        FROM public.game5m_gap_forecast_daily g
        LEFT JOIN public.premarket_daily_features p
          ON p.trade_date = g.trade_date
         AND p.symbol = g.symbol
         AND p.exchange = g.exchange
         AND p.snapshot_label = 'latest'
        WHERE g.trade_date >= CURRENT_DATE - CAST(:days AS integer)
          AND g.open_gap_pct IS NOT NULL
        ORDER BY g.trade_date ASC, g.symbol ASC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"days": max(1, int(days))}).mappings().all()
    return [dict(r) for r in rows]

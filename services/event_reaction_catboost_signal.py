"""
Advisory CatBoost signal for event-reaction (forward 5d log-return after earnings-like events).

Training: scripts/train_event_reaction_catboost.py → .cbm + .meta.json
Features: flat numeric fields from features_before (quotes_regime_v1 / quotes_mvp_1) + symbol.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config_loader import get_config_value
from services.event_reaction_labeling import (
    active_feature_builder_version,
    compute_row_labeling,
    event_reaction_label_threshold_log,
    event_reaction_numeric_feature_keys,
    missing_quote_feature_keys,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _load_model_bundle(model_path: str, model_mtime: float) -> Tuple[Any, Dict[str, Any]]:
    from catboost import CatBoostRegressor

    p = Path(model_path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    meta_path = p.with_suffix(".meta.json")
    if not meta_path.is_file():
        raise FileNotFoundError(str(meta_path))
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    model = CatBoostRegressor()
    model.load_model(str(p))
    return model, meta


def _default_model_path() -> str:
    if Path("/app/logs/ml/models/event_reaction_forward5d_catboost.cbm").is_file():
        return "/app/logs/ml/models/event_reaction_forward5d_catboost.cbm"
    root = Path(__file__).resolve().parents[1]
    return str(root / "local" / "models" / "event_reaction_forward5d_catboost.cbm")


def model_feature_builder_version() -> str | None:
    """Feature builder version stored in the loaded CatBoost .meta.json."""
    status, _, _, bundle = _runtime_guards()
    if status != "ready" or bundle is None:
        return None
    _, meta = bundle
    fbv = str(meta.get("feature_builder_version") or "").strip()
    return fbv or None


def _runtime_guards() -> Tuple[str, str, Optional[str], Optional[Tuple[Any, Dict[str, Any]]]]:
    raw = (get_config_value("EVENT_REACTION_CATBOOST_ENABLED", "false") or "false").strip().lower()
    if raw not in ("1", "true", "yes"):
        return "disabled", "Event-reaction CatBoost выключен (EVENT_REACTION_CATBOOST_ENABLED).", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "Пакет catboost не установлен.", None, None

    model_path = (get_config_value("EVENT_REACTION_CATBOOST_MODEL_PATH", "") or "").strip() or _default_model_path()
    if not os.path.isfile(model_path):
        return "no_model_file", f"Нет файла модели: {model_path}", model_path, None
    try:
        mtime = os.path.getmtime(model_path)
    except OSError:
        mtime = 0.0
    try:
        bundle = _load_model_bundle(model_path, mtime)
    except Exception as e:
        logger.warning("Event-reaction CatBoost load %s: %s", model_path, e)
        return "load_error", f"Ошибка загрузки модели: {e}", model_path, None
    return "ready", "", model_path, bundle


def _score_from_expected_log_return(value: Optional[float], threshold_log: float) -> Optional[float]:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    score = 50.0 + ((x - threshold_log) / 0.05) * 50.0
    return round(max(0.0, min(100.0, score)), 1)


def _provisional_direction_from_log_return(value: float, threshold_log: float) -> str:
    """Temporary UP/DOWN/FLAT label until scenario classifier is trained."""
    if value > threshold_log:
        return "UP"
    if value < -threshold_log:
        return "DOWN"
    return "FLAT"


def _effect_text(direction: str, pred_pct: float, score: Optional[float], threshold_pct: float) -> str:
    if direction == "UP":
        return (
            f"ML ожидает позитивную 5d реакцию около {pred_pct:.2f}% "
            f"(entry_score={score if score is not None else '—'}, порог ±{threshold_pct:.2f}%)."
        )
    if direction == "DOWN":
        return (
            f"ML ожидает негативную 5d реакцию около {pred_pct:.2f}% "
            f"(entry_score={score if score is not None else '—'}, порог ±{threshold_pct:.2f}%)."
        )
    return (
        f"ML ожидает нейтральную 5d реакцию около {pred_pct:.2f}% "
        f"(внутри порога ±{threshold_pct:.2f}%)."
    )


def _features_to_model_row(
    symbol: str,
    features_before: Dict[str, Any],
    *,
    feature_builder_version: str,
) -> Optional[Tuple[List[Any], str]]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    miss = missing_quote_feature_keys(features_before)
    if miss:
        return None
    numeric_keys = event_reaction_numeric_feature_keys(feature_builder_version)
    row: List[Any] = [sym]
    for k in numeric_keys:
        v = features_before.get(k)
        try:
            fv = float(v) if v is not None else float("nan")
        except (TypeError, ValueError):
            fv = float("nan")
        if not math.isfinite(fv):
            if k == "market_regime_present":
                fv = 0.0
            else:
                return None
        row.append(fv)
    return row, sym


def predict_event_reaction_from_features(
    symbol: str,
    features_before: Dict[str, Any],
    *,
    event_time_et: Any = None,
) -> Dict[str, Any]:
    """
    Predict forward_log_ret_5d from a precomputed features_before dict (DB or live builder).
    """
    status, note, model_path, bundle = _runtime_guards()
    base: Dict[str, Any] = {
        "event_reaction_ml_status": status,
        "event_reaction_ml_note": note,
        "event_reaction_ml_model_path": model_path,
    }
    sym = str(symbol or "").strip().upper()
    if status != "ready" or bundle is None:
        return base

    model, meta = bundle
    fbv_meta = str(meta.get("feature_builder_version") or active_feature_builder_version()).strip()
    fbv_feats = str((features_before or {}).get("feature_builder_version") or fbv_meta).strip()
    if fbv_feats != fbv_meta:
        return {
            **base,
            "event_reaction_ml_status": "feature_version_mismatch",
            "event_reaction_ml_note": f"features {fbv_feats} != model {fbv_meta}",
        }

    built = _features_to_model_row(sym, features_before, feature_builder_version=fbv_meta)
    if built is None:
        return {
            **base,
            "event_reaction_ml_status": "incomplete_features",
            "event_reaction_ml_note": "Неполные или невалидные features_before.",
        }
    row, _ = built
    expected_names = list(meta.get("feature_names") or [])
    if expected_names and len(expected_names) != len(row):
        return {
            **base,
            "event_reaction_ml_status": "feature_mismatch",
            "event_reaction_ml_note": "Длина признаков не совпадает с meta.json.",
        }

    try:
        from catboost import Pool

        cat_idx = meta.get("cat_feature_indices") or [0]
        pool = Pool([row], cat_features=cat_idx, feature_names=expected_names or None)
        pred_log = float(model.predict(pool)[0])
    except Exception as e:
        logger.warning("Event-reaction predict %s: %s", sym, e)
        return {**base, "event_reaction_ml_status": "predict_error", "event_reaction_ml_note": str(e)}

    if not math.isfinite(pred_log):
        return {**base, "event_reaction_ml_status": "bad_prediction", "event_reaction_ml_note": "NaN от модели."}

    thr = float(meta.get("threshold_log_return") or event_reaction_label_threshold_log())
    pred_pct = (math.exp(pred_log) - 1.0) * 100.0
    threshold_pct = round((math.exp(thr) - 1.0) * 100.0, 2)
    entry_score = _score_from_expected_log_return(pred_log, thr)
    provisional_direction = _provisional_direction_from_log_return(pred_log, thr)
    metrics = meta.get("metrics") or {}
    rmse_valid = metrics.get("rmse_valid")
    return {
        **base,
        "event_reaction_ml_status": "ok",
        "event_reaction_ml_note": "",
        "event_reaction_ml_symbol": sym,
        "event_reaction_ml_event_time_et": str(event_time_et) if event_time_et is not None else None,
        "event_reaction_ml_feature_builder_version": fbv_meta,
        "event_reaction_ml_expected_log_return_5d": round(pred_log, 6),
        "event_reaction_ml_forward_log_ret_5d_pred": round(pred_log, 6),
        "event_reaction_ml_expected_return_5d_pct": round(pred_pct, 2),
        "event_reaction_ml_entry_score": entry_score,
        "event_reaction_ml_threshold_pct": threshold_pct,
        "event_reaction_ml_direction": provisional_direction,
        "event_reaction_ml_direction_source": "regression_threshold_v0",
        "event_reaction_ml_classifier_status": "temporary_from_regression",
        "event_reaction_ml_effect": _effect_text(provisional_direction, pred_pct, entry_score, threshold_pct),
        "event_reaction_ml_product_readiness": "advisory_ready",
        "event_reaction_ml_product_note": (
            "Готово для карточек/API как advisory; hard-block BUY отключён до trading backtest/live shadow."
        ),
        "event_reaction_ml_rmse_valid": rmse_valid,
    }


def predict_event_reaction_live(symbol: str, event_time_et: Any) -> Dict[str, Any]:
    """Build features from quotes/regime and predict (no outcomes required)."""
    sym = str(symbol or "").strip().upper()
    feats, _, _, reason = compute_row_labeling(sym, event_time_et)
    if not feats:
        return {
            "event_reaction_ml_status": "no_features",
            "event_reaction_ml_note": reason or "compute_row_labeling failed",
            "event_reaction_ml_model_path": (get_config_value("EVENT_REACTION_CATBOOST_MODEL_PATH", "") or "").strip()
            or _default_model_path(),
            "event_reaction_ml_symbol": sym,
            "event_reaction_ml_event_time_et": str(event_time_et) if event_time_et is not None else None,
        }
    out = predict_event_reaction_from_features(sym, feats, event_time_et=event_time_et)
    if reason and reason != "insufficient_forward_for_5d":
        out["event_reaction_ml_labeling_note"] = reason
    return out


def _entry_window_days() -> int:
    raw = (get_config_value("EVENT_REACTION_ENTRY_WINDOW_DAYS", "14") or "14").strip()
    try:
        return max(1, min(60, int(float(raw))))
    except (TypeError, ValueError):
        return 14


def _load_dataset_row_by_date(
    symbol: str,
    event_date: Any,
    *,
    preferred_feature_builder_version: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Load event_reaction_dataset row for symbol on a specific calendar date (ET)."""
    from datetime import date as date_cls, datetime

    from report_generator import get_engine
    from sqlalchemy import text

    sym = str(symbol or "").strip().upper()
    if not sym or event_date is None:
        return None
    if isinstance(event_date, datetime):
        ev_d = event_date.date()
    elif isinstance(event_date, date_cls):
        ev_d = event_date
    else:
        try:
            ev_d = date_cls.fromisoformat(str(event_date).strip()[:10])
        except ValueError:
            return None
    preferred_fbv = (preferred_feature_builder_version or "").strip()
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, symbol, event_time_et, features_before, knowledge_base_id
                    FROM event_reaction_dataset
                    WHERE UPPER(symbol) = :sym
                      AND features_before IS NOT NULL
                      AND features_before <> '{}'::jsonb
                      AND event_time_et IS NOT NULL
                      AND event_time_et::date = :ev_d
                    ORDER BY
                      CASE
                        WHEN :preferred_fbv <> ''
                         AND features_before->>'feature_builder_version' = :preferred_fbv THEN 0
                        ELSE 1
                      END,
                      id DESC
                    LIMIT 1
                    """
                ),
                {"sym": sym, "ev_d": ev_d, "preferred_fbv": preferred_fbv},
            ).mappings().first()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("event_reaction row by date %s %s: %s", sym, ev_d, e)
        return None


def _load_nearest_dataset_row(symbol: str) -> Optional[Dict[str, Any]]:
    from report_generator import get_engine
    from sqlalchemy import text

    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    window = _entry_window_days()
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, symbol, event_time_et, features_before
                    FROM event_reaction_dataset
                    WHERE UPPER(symbol) = :sym
                      AND features_before IS NOT NULL
                      AND features_before <> '{}'::jsonb
                      AND event_time_et IS NOT NULL
                      AND event_time_et >= (NOW() AT TIME ZONE 'America/New_York')::date - :wd * INTERVAL '1 day'
                      AND event_time_et <= (NOW() AT TIME ZONE 'America/New_York')::date + :wd * INTERVAL '1 day'
                    ORDER BY ABS(EXTRACT(EPOCH FROM (event_time_et - NOW())))
                    LIMIT 1
                    """
                ),
                {"sym": sym, "wd": int(window)},
            ).mappings().first()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("event_reaction nearest row %s: %s", sym, e)
        return None


def _json_obj(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def predict_event_reaction_for_ticker(symbol: str, *, event_date: Any = None) -> Dict[str, Any]:
    """
    Prefer materialized features_before from event_reaction_dataset.
    If event_date is set, load that KB row; else nearest row ±window around today.
    """
    sym = str(symbol or "").strip().upper()
    model_fbv = model_feature_builder_version()
    row = (
        _load_dataset_row_by_date(sym, event_date, preferred_feature_builder_version=model_fbv)
        if event_date is not None
        else None
    )
    if row is None and event_date is None:
        row = _load_nearest_dataset_row(sym)
    if row:
        feats = _json_obj(row.get("features_before"))
        if feats:
            out = predict_event_reaction_from_features(
                sym, feats, event_time_et=row.get("event_time_et")
            )
            if out.get("event_reaction_ml_status") == "feature_version_mismatch" and model_fbv:
                rebuilt, _, _, reason = compute_row_labeling(
                    sym,
                    row.get("event_time_et"),
                    knowledge_base_id=row.get("knowledge_base_id"),
                    feature_builder_version=model_fbv,
                )
                if rebuilt:
                    return predict_event_reaction_from_features(
                        sym, rebuilt, event_time_et=row.get("event_time_et")
                    )
                out["event_reaction_ml_note"] = (
                    f"{out.get('event_reaction_ml_note', '')}; rebuild: {reason or 'failed'}"
                ).strip("; ")
            return out
        evt = row.get("event_time_et")
        if evt is not None:
            return predict_event_reaction_live(sym, evt)
    if event_date is not None:
        return {
            "event_reaction_ml_status": "no_features",
            "event_reaction_ml_note": (
                f"Нет features_before в event_reaction_dataset для {sym} на {str(event_date)[:10]}."
            ),
            "event_reaction_ml_symbol": sym,
            "event_reaction_ml_event_date": str(event_date)[:10],
        }
    return {
        "event_reaction_ml_status": "no_event",
        "event_reaction_ml_note": f"Нет строки event_reaction_dataset ±{_entry_window_days()}d.",
        "event_reaction_ml_symbol": sym,
    }

"""Inference for open-path scenario CatBoost classifier (advisory / shadow)."""
from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config_loader import get_config_value
from services.open_path_classifier_dataset import (
    FEATURE_BUILDER_VERSION,
    MODEL_VERSION,
    features_record_from_json,
    open_path_numeric_feature_keys,
)

logger = logging.getLogger(__name__)

SCENARIO_SOURCE_SIGN: dict[str, float] = {
    "open_follow_through_up": 1.0,
    "open_gap_up_fade": -0.5,
    "open_gap_down_bounce": 1.0,
    "open_gap_down_continuation": -1.0,
    "open_flat_chop": 0.0,
    "open_strong_gap_chase": -0.5,
}


def expected_sign_for_scenario(scenario: str) -> Optional[float]:
    s = (scenario or "").strip()
    if not s:
        return None
    v = SCENARIO_SOURCE_SIGN.get(s)
    return float(v) if v is not None else None


def _default_model_path() -> str:
    p = Path("/app/logs/ml/models/open_path_scenario_catboost.cbm")
    if p.is_file():
        return str(p)
    return str(Path(__file__).resolve().parents[1] / "local/models/open_path_scenario_catboost.cbm")


@lru_cache(maxsize=4)
def _load_classifier(model_path: str, mtime: float):
    from catboost import CatBoostClassifier

    model = CatBoostClassifier()
    model.load_model(model_path)
    return model


def _runtime_bundle() -> Tuple[str, str, Optional[str], Any]:
    raw = (get_config_value("OPEN_PATH_SCENARIO_CLASSIFIER_ENABLED", "true") or "true").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return "disabled", "OPEN_PATH_SCENARIO_CLASSIFIER_ENABLED=false", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "catboost not installed", None, None
    path = (get_config_value("OPEN_PATH_SCENARIO_CLASSIFIER_MODEL_PATH", "") or "").strip() or _default_model_path()
    if not os.path.isfile(path):
        return "no_model", f"missing model {path}", path, None
    try:
        mtime = os.path.getmtime(path)
        model = _load_classifier(path, mtime)
    except Exception as e:
        return "load_error", str(e), path, None
    return "ready", "", path, model


def predict_open_path_from_features(
    features_before: Any,
    *,
    symbol: str,
) -> dict[str, Any]:
    status, detail, model_path, model = _runtime_bundle()
    out: dict[str, Any] = {
        "open_path_classifier_status": status,
        "open_path_classifier_detail": detail or None,
        "model_version": MODEL_VERSION,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "model_path": model_path,
    }
    if status != "ready" or model is None:
        return out

    rec = features_record_from_json(features_before, symbol=symbol)
    if rec is None:
        out["open_path_classifier_status"] = "no_features"
        out["open_path_classifier_detail"] = f"missing {FEATURE_BUILDER_VERSION} features"
        return out

    feature_names = list(open_path_numeric_feature_keys()) + ["symbol"]
    row = [[rec.get(k, 0.0) for k in open_path_numeric_feature_keys()] + [rec["symbol"]]]
    try:
        from catboost import Pool

        pool = Pool(row, cat_features=[len(feature_names) - 1], feature_names=feature_names)
        pred = model.predict(pool)
        proba = model.predict_proba(pool)[0]
        classes = list(model.classes_)
        scenario = str(pred.reshape(-1)[0])
        proba_map = {str(c): round(float(p), 4) for c, p in zip(classes, proba)}
        out.update(
            {
                "open_path_classifier_status": "ok",
                "predicted_scenario": scenario,
                "predicted_scenario_proba": proba_map.get(scenario),
                "predicted_scenario_proba_map": proba_map,
                "predicted_scenario_sign": expected_sign_for_scenario(scenario),
            }
        )
    except Exception as e:
        logger.debug("open_path predict %s: %s", symbol, e)
        out["open_path_classifier_status"] = "predict_error"
        out["open_path_classifier_detail"] = str(e)
    return out


def predict_open_path_from_json(features_before: Any, *, symbol: str) -> dict[str, Any]:
    if isinstance(features_before, str):
        try:
            features_before = json.loads(features_before)
        except Exception:
            features_before = None
    return predict_open_path_from_features(features_before, symbol=symbol)

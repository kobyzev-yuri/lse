"""Inference for earnings scenario CatBoost classifier (advisory / shadow)."""
from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config_loader import get_config_value
from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_EARNINGS,
    event_reaction_numeric_feature_keys,
)

logger = logging.getLogger(__name__)

MODEL_VERSION = "event_reaction_scenario_v0"

# Expected direction of source-ticker 5d log-return (heuristic for shadow / trading gate).
SCENARIO_SOURCE_SIGN: dict[str, float] = {
    "gap_up_follow_through": 1.0,
    "beat_selloff_pullback": -0.5,
    "beat_revaluation_down": -1.0,
    "miss_or_guide_breakdown": -1.0,
    "gap_up_fade": -0.3,
    "cross_earnings_contagion": 0.0,
    "capex_positive_for_infra_peers": -0.5,
}


def _default_model_path() -> str:
    p = Path("/app/logs/ml/models/event_reaction_scenario_catboost.cbm")
    if p.is_file():
        return str(p)
    return str(Path(__file__).resolve().parents[1] / "local/models/event_reaction_scenario_catboost.cbm")


def _json_obj(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


@lru_cache(maxsize=4)
def _load_classifier(model_path: str, mtime: float):
    from catboost import CatBoostClassifier, Pool

    model = CatBoostClassifier()
    model.load_model(model_path)
    return model


def _runtime_bundle() -> Tuple[str, str, Optional[str], Any]:
    raw = (get_config_value("EARNINGS_SCENARIO_CLASSIFIER_ENABLED", "true") or "true").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return "disabled", "EARNINGS_SCENARIO_CLASSIFIER_ENABLED=false", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "catboost not installed", None, None
    path = (get_config_value("EARNINGS_SCENARIO_CLASSIFIER_MODEL_PATH", "") or "").strip() or _default_model_path()
    if not os.path.isfile(path):
        return "no_model", f"missing model {path}", path, None
    try:
        mtime = os.path.getmtime(path)
        model = _load_classifier(path, mtime)
    except Exception as e:
        return "load_error", str(e), path, None
    return "ready", "", path, model


def features_record_from_json(
    features_before: Any,
    *,
    symbol: str,
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
) -> Optional[Dict[str, float]]:
    fb = _json_obj(features_before)
    if not fb or str(fb.get("feature_builder_version") or "") != feature_builder_version:
        return None
    keys = event_reaction_numeric_feature_keys(feature_builder_version)
    rec: Dict[str, float] = {"symbol": symbol.strip().upper()}
    for k in keys:
        try:
            v = float(fb.get(k)) if fb.get(k) is not None else 0.0
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            v = 0.0
        rec[k] = v
    return rec


def predict_scenario_from_features(
    symbol: str,
    features_before: Any,
    *,
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
) -> Dict[str, Any]:
    sym = symbol.strip().upper()
    base: Dict[str, Any] = {
        "scenario_classifier_status": "pending",
        "scenario_classifier_model_version": MODEL_VERSION,
        "predicted_scenario": None,
        "predicted_scenario_proba": None,
        "predicted_scenario_sign": None,
        "scenario_class_probs": {},
    }
    st, note, path, model = _runtime_bundle()
    base["scenario_classifier_model_path"] = path
    if st != "ready" or model is None:
        base["scenario_classifier_status"] = st
        base["scenario_classifier_note"] = note
        return base

    rec = features_record_from_json(
        features_before, symbol=sym, feature_builder_version=feature_builder_version
    )
    if not rec:
        base["scenario_classifier_status"] = "no_features"
        base["scenario_classifier_note"] = f"missing or wrong feature_builder ({feature_builder_version})"
        return base

    feature_names = list(event_reaction_numeric_feature_keys(feature_builder_version)) + ["symbol"]
    row = {k: rec.get(k, 0.0) for k in feature_names[:-1]}
    row["symbol"] = sym
    try:
        from catboost import Pool

        pool = Pool(
            data=[[row[k] for k in feature_names]],
            cat_features=[feature_names.index("symbol")],
            feature_names=feature_names,
        )
        pred = model.predict(pool)
        label = str(pred.reshape(-1)[0]).strip()
        proba_raw = model.predict_proba(pool)[0]
        classes = [str(c) for c in model.classes_]
        prob_map = {c: round(float(p), 4) for c, p in zip(classes, proba_raw)}
        best_p = prob_map.get(label)
        base.update(
            {
                "scenario_classifier_status": "ok",
                "predicted_scenario": label,
                "predicted_scenario_proba": best_p,
                "predicted_scenario_sign": SCENARIO_SOURCE_SIGN.get(label),
                "scenario_class_probs": prob_map,
            }
        )
    except Exception as e:
        logger.warning("scenario predict %s: %s", sym, e)
        base["scenario_classifier_status"] = "predict_error"
        base["scenario_classifier_note"] = str(e)
    return base


def expected_sign_for_scenario(scenario: str | None) -> Optional[float]:
    if not scenario:
        return None
    return SCENARIO_SOURCE_SIGN.get(str(scenario).strip())

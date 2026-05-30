"""Runtime inference for peer spillover CatBoost regressor (Phase C)."""
from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_loader import get_config_value
from services.event_reaction_labeling import FEATURE_BUILDER_VERSION_EARNINGS
from services.peer_spillover_dataset import (
    peer_spillover_categorical_features,
    peer_spillover_feature_names,
)

logger = logging.getLogger(__name__)

MODEL_VERSION = "peer_spillover_forward5d_v0"


def _default_model_path() -> str:
    cfg = (get_config_value("PEER_SPILLOVER_MODEL_PATH", "") or "").strip()
    if cfg:
        return cfg
    p = Path("/app/logs/ml/models/peer_spillover_forward5d_catboost.cbm")
    if p.is_file():
        return str(p)
    return str(Path(__file__).resolve().parents[1] / "local/models/peer_spillover_forward5d_catboost.cbm")


@lru_cache(maxsize=4)
def _load_model(model_path: str, mtime: float):
    from catboost import CatBoostRegressor

    model = CatBoostRegressor()
    model.load_model(model_path)
    return model


def _runtime_bundle() -> tuple[str, str, Optional[str], Any]:
    raw = (get_config_value("PEER_SPILLOVER_MODEL_ENABLED", "true") or "true").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return "disabled", "PEER_SPILLOVER_MODEL_ENABLED=false", None, None
    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "catboost not installed", None, None
    path = _default_model_path()
    if not os.path.isfile(path):
        return "no_model", f"missing model {path}", path, None
    try:
        mtime = os.path.getmtime(path)
        model = _load_model(path, mtime)
    except Exception as e:
        return "load_error", str(e), path, None
    return "ready", "", path, model


def _parse_features(features_before: Any) -> dict[str, Any]:
    if isinstance(features_before, dict):
        return features_before
    if isinstance(features_before, str):
        try:
            o = json.loads(features_before)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def predict_peer_spillover(
    *,
    source_symbol: str,
    peer_ticker: str,
    features_before: Any,
    edge_weight: float = 0.5,
    relation_type: str = "unknown",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
) -> Dict[str, Any]:
    """Predict peer 5d log-return from pre-event source features + edge metadata."""
    src = str(source_symbol or "").strip().upper()
    peer = str(peer_ticker or "").strip().upper()
    base: Dict[str, Any] = {
        "peer_spillover_ml_status": "pending",
        "peer_spillover_ml_model_version": MODEL_VERSION,
        "source_symbol": src,
        "peer_ticker": peer,
        "edge_weight": edge_weight,
        "relation_type": relation_type,
    }
    if not src or not peer:
        base["peer_spillover_ml_status"] = "missing_symbol"
        return base

    status, note, path, model = _runtime_bundle()
    base["peer_spillover_ml_model_path"] = path
    if status != "ready" or model is None:
        base["peer_spillover_ml_status"] = status
        base["peer_spillover_ml_note"] = note
        return base

    fb = _parse_features(features_before)
    if str(fb.get("feature_builder_version") or "") != feature_builder_version:
        base["peer_spillover_ml_status"] = "feature_version_mismatch"
        base["peer_spillover_ml_note"] = (
            f"expected {feature_builder_version}, got {fb.get('feature_builder_version')}"
        )
        return base

    feature_names = peer_spillover_feature_names(feature_builder_version=feature_builder_version)
    row: dict[str, Any] = {
        "edge_weight": float(edge_weight),
        "source_symbol": src,
        "peer_ticker": peer,
        "relation_type": str(relation_type or "unknown"),
    }
    for k in feature_names:
        if k in peer_spillover_categorical_features() or k == "edge_weight":
            continue
        try:
            row[k] = float(fb.get(k)) if fb.get(k) is not None else 0.0
        except (TypeError, ValueError):
            row[k] = 0.0

    try:
        from catboost import Pool

        pool = Pool(
            data=[[row[k] for k in feature_names]],
            cat_features=[feature_names.index(c) for c in peer_spillover_categorical_features()],
            feature_names=feature_names,
        )
        pred = float(model.predict(pool).reshape(-1)[0])
        base.update(
            {
                "peer_spillover_ml_status": "ok",
                "peer_forward_log_ret_5d_pred": round(pred, 6),
                "peer_forward_simple_pct_pred": round((math.exp(pred) - 1.0) * 100.0, 4),
            }
        )
    except Exception as e:
        logger.warning("peer spillover predict %s→%s: %s", src, peer, e)
        base["peer_spillover_ml_status"] = "predict_error"
        base["peer_spillover_ml_note"] = str(e)
    return base


def predict_peer_spillover_batch(
    *,
    source_symbol: str,
    features_before: Any,
    peer_edges: List[dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for edge in peer_edges or []:
        peer = str(edge.get("target_ticker") or edge.get("peer_ticker") or "").upper()
        if not peer or edge.get("relation_type") == "sector_etf":
            continue
        out.append(
            predict_peer_spillover(
                source_symbol=source_symbol,
                peer_ticker=peer,
                features_before=features_before,
                edge_weight=float(edge.get("weight") or 0.5),
                relation_type=str(edge.get("relation_type") or "unknown"),
            )
        )
    return out

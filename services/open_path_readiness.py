"""Readiness gates for open-path scenario classifier (GAME_5M)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.open_path_classifier_dataset import collect_open_path_classifier_coverage

logger = logging.getLogger(__name__)

DEFAULT_SINCE = "2026-01-01"


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _json_load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def default_readiness_metrics_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_open_path_readiness.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_open_path_readiness.json"


def default_train_metrics_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_open_path_scenario_train_metrics.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_open_path_scenario_train_metrics.json"


def default_dataset_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/open_path_dataset.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "open_path_dataset.json"


def default_shadow_report_path(project_root: Path | None = None) -> Path:
    from services.open_path_scenario_shadow import default_shadow_report_path as _p

    return _p(project_root)


def collect_open_path_readiness_snapshot(
    engine: Engine,
    *,
    since: str = DEFAULT_SINCE,
) -> Dict[str, Any]:
    from services.open_path_classifier_dataset import collect_open_path_data_counts

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "open_path_data": collect_open_path_data_counts(engine),
        "open_path_classifier_dataset": collect_open_path_classifier_coverage(engine, since=since),
    }


def gate_open_path_classifier_dataset(data: Dict[str, Any]) -> Dict[str, Any]:
    reasons: list[str] = []
    sc = data.get("open_path_classifier_dataset") if isinstance(data.get("open_path_classifier_dataset"), dict) else {}
    min_labels = _cfg_int("ML_READINESS_OPEN_PATH_MIN_TRAIN_ROWS", 200)
    min_trainable = _cfg_int("ML_READINESS_OPEN_PATH_MIN_TRAINABLE_ROWS", 150)
    min_classes = _cfg_int("ML_READINESS_OPEN_PATH_MIN_CLASSES", 4)
    max_sparse = _cfg_int("ML_READINESS_OPEN_PATH_MAX_SPARSE_CLASSES", 2)
    max_unlabeled = _cfg_int("ML_READINESS_OPEN_PATH_MAX_GAP_OPEN_UNLABELED", 30)
    max_no_features = _cfg_int("ML_READINESS_OPEN_PATH_MAX_LABELS_WITHOUT_FEATURES", 0)

    n_labels = int(sc.get("n_rule_labels") or 0)
    n_trainable = int(sc.get("n_trainable_rows") or 0)
    n_classes = int(sc.get("n_classes_distinct") or 0)
    sparse = sc.get("sparse_classes_below_min_samples") or []
    n_unlabeled = int(sc.get("n_gap_open_unlabeled") or 0)
    no_features = int(sc.get("labels_without_features") or 0)

    if n_labels < min_labels:
        reasons.append(f"n_rule_labels<{min_labels}")
    if n_trainable < min_trainable:
        reasons.append(f"n_trainable_rows<{min_trainable}")
    if n_classes < min_classes:
        reasons.append(f"n_classes<{min_classes}")
    if len(sparse) > max_sparse:
        reasons.append(f"sparse_classes>{max_sparse}")
    if n_unlabeled > max_unlabeled:
        reasons.append(f"gap_open_unlabeled>{max_unlabeled}")
    if no_features > max_no_features:
        reasons.append(f"labels_without_features>{max_no_features}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "n_rule_labels": n_labels,
        "n_trainable_rows": n_trainable,
        "n_classes_distinct": n_classes,
        "labels_by_class": sc.get("labels_by_class"),
        "sparse_classes_below_min_samples": sparse[:8],
        "n_gap_open_unlabeled": n_unlabeled,
        "labels_without_features": no_features,
        "advisory_only": True,
    }


def gate_open_path_classifier(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not metrics:
        return {"ready": False, "reasons": ["no_open_path_metrics_file"], "valid_accuracy": None, "n_train": 0}
    st = metrics.get("status")
    if st not in ("ok",):
        reasons.append(f"status={st}")
    mets = metrics.get("metrics") if isinstance(metrics.get("metrics"), dict) else metrics
    n_train = int(mets.get("n_train") or metrics.get("n_train") or 0)
    min_train = _cfg_int("ML_READINESS_OPEN_PATH_CLASSIFIER_MIN_TRAIN", 120)
    min_acc = _cfg_float("ML_READINESS_OPEN_PATH_CLASSIFIER_MIN_ACCURACY", 0.35)
    if n_train < min_train:
        reasons.append(f"n_train<{min_train}")
    acc = mets.get("valid_accuracy")
    if acc is None:
        reasons.append("no_valid_accuracy")
    elif float(acc) < min_acc:
        reasons.append(f"valid_accuracy<{min_acc}")
    if mets.get("holdout_skipped"):
        reasons.append("holdout_skipped")
    classes = mets.get("classes") or []
    min_classes = _cfg_int("ML_READINESS_OPEN_PATH_MIN_CLASSES", 4)
    if isinstance(classes, list) and len(classes) < min_classes:
        reasons.append(f"n_classes<{min_classes}")
    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "valid_accuracy": acc,
        "n_train": n_train,
        "n_classes": len(classes) if isinstance(classes, list) else None,
        "holdout_skipped": bool(mets.get("holdout_skipped")),
        "status": st,
        "advisory_only": True,
    }


def gate_open_path_trading_shadow(shadow_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not shadow_report:
        return {"ready": False, "reasons": ["no_open_path_shadow_file"], "sign_accuracy": None, "class_accuracy": None}
    gate = shadow_report.get("trading_gate") if isinstance(shadow_report.get("trading_gate"), dict) else {}
    agg = shadow_report.get("aggregate") if isinstance(shadow_report.get("aggregate"), dict) else {}
    if gate:
        return {
            "ready": bool(gate.get("ready")),
            "reasons": list(gate.get("reasons") or []),
            "sign_accuracy": agg.get("sign_accuracy"),
            "class_accuracy": agg.get("class_accuracy"),
            "n_matured": agg.get("n_matured"),
            "mean_pseudo_pnl_log": agg.get("mean_pseudo_pnl_log"),
            "advisory_only": True,
        }
    from services.open_path_scenario_shadow import compute_open_path_trading_metric_gate

    computed = compute_open_path_trading_metric_gate(agg)
    return {
        "ready": bool(computed.get("ready")),
        "reasons": list(computed.get("reasons") or []),
        "sign_accuracy": agg.get("sign_accuracy"),
        "class_accuracy": agg.get("class_accuracy"),
        "n_matured": agg.get("n_matured"),
        "mean_pseudo_pnl_log": agg.get("mean_pseudo_pnl_log"),
        "advisory_only": True,
    }


def build_open_path_gates(
    snapshot: Dict[str, Any],
    *,
    train_metrics: Optional[Dict[str, Any]] = None,
    shadow_report: Optional[Dict[str, Any]] = None,
    prerequisites_ready: bool = False,
) -> Dict[str, Any]:
    g_dataset = gate_open_path_classifier_dataset(snapshot)
    g_classifier = gate_open_path_classifier(train_metrics)
    g_shadow = gate_open_path_trading_shadow(shadow_report)
    classifier_ready = bool(g_dataset.get("ready")) and bool(g_classifier.get("ready"))
    product_ready = classifier_ready and bool(g_shadow.get("ready")) and bool(prerequisites_ready)
    return {
        "open_path_classifier_dataset": g_dataset,
        "open_path_classifier": g_classifier,
        "open_path_trading_shadow": g_shadow,
        "overall_open_path_classifier_ready": product_ready,
        "overall_open_path_classifier_model_ready": classifier_ready,
    }


def write_open_path_readiness(
    engine: Engine,
    *,
    project_root: Path | None = None,
    train_metrics_path: Path | None = None,
    dataset_path: Path | None = None,
    since: str = DEFAULT_SINCE,
    prerequisites_ready: Optional[bool] = None,
    earnings_snapshot: Optional[Dict[str, Any]] = None,
    earnings_shadow_aggregate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root = project_root or Path(__file__).resolve().parents[1]
    snap = collect_open_path_readiness_snapshot(engine, since=since)
    metrics_path = train_metrics_path or default_train_metrics_path(root)
    ds_path = dataset_path or default_dataset_path(root)
    train_data = _json_load(metrics_path)
    shadow_data = _json_load(default_shadow_report_path(root))

    prereq = prerequisites_ready
    if prereq is None:
        try:
            from services.earnings_intelligence_readiness import default_readiness_metrics_path as earnings_readiness_path

            ei = _json_load(earnings_readiness_path(root))
            prereq = bool((ei or {}).get("gates", {}).get("overall_open_path_mvp_prerequisites_ready"))
            if earnings_snapshot is None and ei:
                earnings_snapshot = (ei.get("snapshot") or {}) if isinstance(ei.get("snapshot"), dict) else None
        except Exception:
            prereq = False

    if earnings_shadow_aggregate is None:
        try:
            from services.earnings_intelligence_readiness import default_shadow_report_path as earnings_shadow_path

            eraw = _json_load(earnings_shadow_path(root))
            if isinstance(eraw, dict):
                earnings_shadow_aggregate = eraw.get("aggregate") if isinstance(eraw.get("aggregate"), dict) else None
        except Exception:
            earnings_shadow_aggregate = None

    gates = build_open_path_gates(
        snap,
        train_metrics=train_data,
        shadow_report=shadow_data,
        prerequisites_ready=bool(prereq),
    )

    from services.open_path_product_eta import (
        append_readiness_history,
        build_continuous_learning_status,
        collect_open_path_accumulation_window,
        estimate_open_path_product_eta,
    )

    lookback = _cfg_int("OPEN_PATH_ETA_LOOKBACK_DAYS", 21)
    accumulation = collect_open_path_accumulation_window(engine, lookback_days=lookback)
    product_eta = estimate_open_path_product_eta(
        snapshot=snap,
        gates={**gates, "open_path_mvp_prerequisites": {"ready": bool(prereq)}},
        earnings_snapshot=earnings_snapshot,
        accumulation=accumulation,
        shadow_aggregate=(shadow_data or {}).get("aggregate") if isinstance(shadow_data, dict) else None,
        lookback_days=lookback,
    )
    continuous = build_continuous_learning_status(
        project_root=root,
        product_ready=bool(gates.get("overall_open_path_classifier_ready")),
        gates=gates,
        train_metrics=train_data,
    )

    bundle = {
        "readiness_version": "open_path_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot": snap,
        "gates": gates,
        "product_eta": product_eta,
        "continuous_learning": continuous,
        "metrics_paths": {
            "open_path_classifier": str(metrics_path),
            "open_path_dataset": str(ds_path),
            "open_path_shadow": str(default_shadow_report_path(root)),
        },
        "prerequisites_ready": bool(prereq),
    }
    out_path = default_readiness_metrics_path(root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    op_ds = snap.get("open_path_classifier_dataset") if isinstance(snap.get("open_path_classifier_dataset"), dict) else {}
    od = snap.get("open_path_data") if isinstance(snap.get("open_path_data"), dict) else {}
    try:
        append_readiness_history(
            project_root=root,
            metrics={
                "ts_utc": bundle["generated_at_utc"],
                "pm_days": od.get("premarket_feature_trading_days"),
                "rule_labels": op_ds.get("n_rule_labels"),
                "trainable": op_ds.get("n_trainable_rows"),
                "product_ready": gates.get("overall_open_path_classifier_ready"),
                "eta_days": product_eta.get("eta_days_calendar_max"),
            },
        )
    except Exception as e_hist:
        logger.debug("open_path history append: %s", e_hist)

    logger.info(
        "Wrote open-path readiness → %s product_ready=%s model_ready=%s eta_days=%s",
        out_path,
        gates.get("overall_open_path_classifier_ready"),
        gates.get("overall_open_path_classifier_model_ready"),
        product_eta.get("eta_days_calendar_max"),
    )
    return bundle

"""Readiness gates for earnings intelligence ML grid (sources → features → train)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.earnings_intelligence_universe import get_earnings_intelligence_universe

logger = logging.getLogger(__name__)

DEFAULT_DATASET_VERSION = "v0_expanded_baseline"
DEFAULT_FEATURE_BUILDER = "quotes_regime_earnings_v1"
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
        return Path("/app/logs/ml/ml_data_quality/last_earnings_intelligence_readiness.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_earnings_intelligence_readiness.json"


def default_shadow_report_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_earnings_scenario_shadow.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_earnings_scenario_shadow.json"


def default_scenario_train_metrics_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_event_reaction_scenario_train_metrics.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_event_reaction_scenario_train_metrics.json"


def default_peer_spillover_train_metrics_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_peer_spillover_train_metrics.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_peer_spillover_train_metrics.json"


def default_peer_spillover_dataset_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/peer_spillover_dataset.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "peer_spillover_dataset.json"


def collect_earnings_intelligence_readiness(
    engine: Engine,
    *,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    feature_builder_version: str = DEFAULT_FEATURE_BUILDER,
    since: str = DEFAULT_SINCE,
) -> Dict[str, Any]:
    """DB snapshot + optional file metrics for analyzer / cron gates."""
    universe = get_earnings_intelligence_universe()
    sym_set = sorted({s.strip().upper() for s in universe if s.strip()})
    out: Dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset_version,
        "feature_builder_version": feature_builder_version,
        "since": since,
        "universe_size": len(sym_set),
        "universe": sym_set,
        "error": None,
    }
    try:
        with engine.connect() as conn:
            covered = {
                str(r[0]).upper()
                for r in conn.execute(
                    text(
                        """
                        SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
                        FROM earnings_material
                        WHERE parse_status IN ('parsed', 'extracted')
                          AND UPPER(TRIM(symbol)) = ANY(:symbols)
                        """
                    ),
                    {"symbols": sym_set},
                ).all()
            }
            events_q = text(
                """
                SELECT
                  COUNT(*) AS events_total,
                  COUNT(*) FILTER (
                    WHERE EXISTS (
                      SELECT 1 FROM earnings_material em
                      WHERE UPPER(em.symbol) = UPPER(TRIM(kb.ticker))
                        AND em.event_date = kb.ts::date
                        AND em.parse_status IN ('parsed', 'extracted')
                    )
                  ) AS events_with_materials,
                  COUNT(*) FILTER (WHERE ed.guidance_summary->>'management_tone' IS NOT NULL) AS events_with_llm_tone,
                  COUNT(*) FILTER (WHERE ed.guidance_summary ? 'scenario_hints') AS events_with_scenario_hints
                FROM knowledge_base kb
                LEFT JOIN earnings_event_detail ed ON ed.knowledge_base_id = kb.id
                WHERE UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
                  AND UPPER(TRIM(kb.ticker)) = ANY(:symbols)
                  AND kb.ts::date >= CAST(:since AS date)
                  AND kb.ts::date <= CURRENT_DATE
                """
            )
            ev = conn.execute(events_q, {"symbols": sym_set, "since": since}).mappings().first() or {}
            peer_rows = int(conn.execute(text("SELECT COUNT(*) FROM peer_graph_edge")).scalar() or 0)
            erd_q = text(
                """
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE label_source = 'llm_scenario_v0') AS llm_scenario_labels,
                  COUNT(*) FILTER (
                    WHERE final_label IS NOT NULL
                      AND TRIM(final_label) <> ''
                      AND final_label NOT IN ('UP','DOWN','FLAT')
                  ) AS named_scenario_labels,
                  COUNT(*) FILTER (
                    WHERE features_before IS NOT NULL
                      AND features_before <> '{}'::jsonb
                      AND (features_before->>'feature_builder_version') = :fbv
                  ) AS with_earnings_features
                FROM event_reaction_dataset
                WHERE dataset_version = :dv
                """
            )
            erd = conn.execute(erd_q, {"dv": dataset_version, "fbv": feature_builder_version}).mappings().first() or {}
            llm_symbols = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT UPPER(TRIM(kb.ticker)))
                        FROM earnings_event_detail ed
                        JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
                        WHERE ed.guidance_summary->>'management_tone' IS NOT NULL
                          AND UPPER(TRIM(kb.ticker)) = ANY(:symbols)
                        """
                    ),
                    {"symbols": sym_set},
                ).scalar()
                or 0
            )

        events_total = int(ev.get("events_total") or 0)
        events_with_materials = int(ev.get("events_with_materials") or 0)
        events_with_llm = int(ev.get("events_with_llm_tone") or 0)
        events_with_hints = int(ev.get("events_with_scenario_hints") or 0)
        missing_materials = [s for s in sym_set if s not in covered]

        out["sources"] = {
            "symbols_with_materials": len(covered),
            "symbols_without_materials": missing_materials,
            "materials_symbol_coverage_rate": round(len(covered) / len(sym_set), 4) if sym_set else 0.0,
            "events_since": events_total,
            "events_with_materials": events_with_materials,
            "events_materials_rate": round(events_with_materials / events_total, 4) if events_total else 0.0,
            "events_with_llm_tone": events_with_llm,
            "events_llm_rate": round(events_with_llm / events_total, 4) if events_total else 0.0,
            "events_with_scenario_hints": events_with_hints,
            "llm_symbols_distinct": llm_symbols,
            "peer_graph_edge_rows": peer_rows,
        }
        out["labels_and_features"] = {
            "event_reaction_rows": int(erd.get("total") or 0),
            "llm_scenario_labels": int(erd.get("llm_scenario_labels") or 0),
            "named_scenario_labels": int(erd.get("named_scenario_labels") or 0),
            "earnings_v1_feature_rows": int(erd.get("with_earnings_features") or 0),
        }
        from services.peer_spillover_dataset import collect_peer_spillover_coverage
        from services.scenario_classifier_dataset import collect_scenario_classifier_coverage

        min_peer_samples = _cfg_int("ML_READINESS_PEER_SPILLOVER_MIN_PEER_SAMPLES", 2)
        min_class_samples = _cfg_int("ML_READINESS_SCENARIO_MIN_CLASS_SAMPLES", 2)
        out["peer_spillover_dataset"] = collect_peer_spillover_coverage(
            engine,
            dataset_version=dataset_version,
            feature_builder_version=feature_builder_version,
            since=since,
            universe=sym_set,
            min_peer_samples=min_peer_samples,
        )
        out["scenario_classifier_dataset"] = collect_scenario_classifier_coverage(
            engine,
            dataset_version=dataset_version,
            feature_builder_version=feature_builder_version,
            since=since,
            universe=sym_set,
            min_class_samples=min_class_samples,
        )
        try:
            from services.open_path_classifier_dataset import (
                collect_open_path_classifier_coverage,
                collect_open_path_data_counts,
            )

            out["open_path_classifier_dataset"] = collect_open_path_classifier_coverage(engine, since=since)
            out["open_path_data"] = collect_open_path_data_counts(engine)
        except Exception as e_opc:
            logger.debug("open_path_classifier_dataset: %s", e_opc)
            out["open_path_classifier_dataset"] = {"error": str(e_opc)}
            out["open_path_data"] = {"error": str(e_opc)}
    except Exception as e:
        out["error"] = str(e)
        logger.warning("earnings intelligence readiness: %s", e)
    return out


def gate_sources(data: Dict[str, Any]) -> Dict[str, Any]:
    reasons: list[str] = []
    src = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    min_sym_cov = _cfg_float("ML_READINESS_EARNINGS_MIN_SYMBOL_MATERIALS_RATE", 0.35)
    min_events_llm = _cfg_float("ML_READINESS_EARNINGS_MIN_EVENTS_LLM_RATE", 0.25)
    min_peer = _cfg_int("ML_READINESS_EARNINGS_MIN_PEER_GRAPH_ROWS", 20)

    sym_rate = float(src.get("materials_symbol_coverage_rate") or 0.0)
    llm_rate = float(src.get("events_llm_rate") or 0.0)
    peer_rows = int(src.get("peer_graph_edge_rows") or 0)

    if sym_rate < min_sym_cov:
        reasons.append(f"materials_symbol_coverage<{min_sym_cov}")
    if llm_rate < min_events_llm:
        reasons.append(f"events_llm_rate<{min_events_llm}")
    if peer_rows < min_peer:
        reasons.append(f"peer_graph_rows<{min_peer}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "materials_symbol_coverage_rate": sym_rate,
        "events_llm_rate": llm_rate,
        "peer_graph_edge_rows": peer_rows,
    }


def gate_features(data: Dict[str, Any]) -> Dict[str, Any]:
    reasons: list[str] = []
    lf = data.get("labels_and_features") if isinstance(data.get("labels_and_features"), dict) else {}
    min_rows = _cfg_int("ML_READINESS_EARNINGS_MIN_FEATURE_ROWS", 8)
    n = int(lf.get("earnings_v1_feature_rows") or 0)
    if n < min_rows:
        reasons.append(f"earnings_v1_feature_rows<{min_rows}")
    return {"ready": len(reasons) == 0, "reasons": reasons, "earnings_v1_feature_rows": n}


def gate_scenario_labels(data: Dict[str, Any]) -> Dict[str, Any]:
    reasons: list[str] = []
    lf = data.get("labels_and_features") if isinstance(data.get("labels_and_features"), dict) else {}
    min_labels = _cfg_int("ML_READINESS_EARNINGS_MIN_SCENARIO_LABELS", 8)
    n = int(lf.get("llm_scenario_labels") or 0)
    if n < min_labels:
        reasons.append(f"llm_scenario_labels<{min_labels}")
    return {"ready": len(reasons) == 0, "reasons": reasons, "llm_scenario_labels": n}


def gate_scenario_classifier(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not metrics:
        return {"ready": False, "reasons": ["no_scenario_metrics_file"], "valid_accuracy": None, "n_train": 0}
    st = metrics.get("status")
    if st not in ("ok",):
        reasons.append(f"status={st}")
    mets = metrics.get("metrics") if isinstance(metrics.get("metrics"), dict) else metrics
    n_train = int(mets.get("n_train") or metrics.get("n_train") or 0)
    n_valid = int(mets.get("n_valid") or 0)
    min_train = _cfg_int("ML_READINESS_EARNINGS_SCENARIO_MIN_TRAIN", 6)
    min_valid = _cfg_int("ML_READINESS_SCENARIO_MIN_VALID", 4)
    min_classes = _cfg_int("ML_READINESS_SCENARIO_MIN_CLASSES", 3)
    if n_train < min_train:
        reasons.append(f"n_train<{min_train}")
    holdout_skipped = bool(mets.get("holdout_skipped"))
    # Pilot: при sparse classes train идёт на всей выборке без eval — не блокируем grid.
    if not holdout_skipped:
        if n_valid < min_valid:
            reasons.append(f"n_valid<{min_valid}")
    classes = mets.get("classes") or []
    if isinstance(classes, list) and len(classes) < min_classes:
        reasons.append(f"n_classes<{min_classes}")
    acc = mets.get("valid_accuracy")
    if acc is not None:
        min_acc = _cfg_float("ML_READINESS_EARNINGS_SCENARIO_MIN_ACCURACY", 0.0)
        if min_acc > 0 and float(acc) < min_acc:
            reasons.append(f"valid_accuracy<{min_acc}")
    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "valid_accuracy": acc,
        "n_train": n_train,
        "n_valid": n_valid,
        "n_classes": len(classes) if isinstance(classes, list) else None,
        "holdout_skipped": bool(mets.get("holdout_skipped")),
        "status": st,
    }


def gate_scenario_classifier_dataset(data: Dict[str, Any]) -> Dict[str, Any]:
    reasons: list[str] = []
    sc = data.get("scenario_classifier_dataset") if isinstance(data.get("scenario_classifier_dataset"), dict) else {}
    min_labels = _cfg_int("ML_READINESS_EARNINGS_MIN_SCENARIO_LABELS", 8)
    min_trainable = _cfg_int("ML_READINESS_SCENARIO_MIN_TRAINABLE_ROWS", 18)
    min_classes = _cfg_int("ML_READINESS_SCENARIO_MIN_CLASSES", 3)
    max_symbols_no_label = _cfg_int("ML_READINESS_SCENARIO_MAX_SYMBOLS_WITHOUT_LABELS", 12)
    max_hints_pending = _cfg_int("ML_READINESS_SCENARIO_MAX_HINTS_PENDING", 3)
    max_missing_extract = _cfg_int("ML_READINESS_SCENARIO_MAX_MISSING_EXTRACT", 8)
    max_label_no_features = _cfg_int("ML_READINESS_SCENARIO_MAX_LABEL_NO_FEATURES", 0)
    max_sparse_classes = _cfg_int("ML_READINESS_SCENARIO_MAX_SPARSE_CLASSES", 2)

    n_labels = int(sc.get("n_llm_labels") or 0)
    n_trainable = int(sc.get("n_trainable_rows") or 0)
    n_classes = int(sc.get("n_classes_distinct") or 0)
    symbols_without = sc.get("symbols_without_labels") or []
    hints_pending = sc.get("hints_pending_apply") or []
    extract_missing = sc.get("events_missing_llm_extract") or []
    sparse = sc.get("sparse_classes_below_min_samples") or []
    label_no_features = int(sc.get("labels_without_earnings_v1_features") or 0)
    features_gap = int(sc.get("features_gap_rows") or 0)

    if n_labels < min_labels:
        reasons.append(f"n_llm_labels<{min_labels}")
    if n_trainable < min_trainable:
        reasons.append(f"n_trainable_rows<{min_trainable}")
    if n_classes < min_classes:
        reasons.append(f"n_classes<{min_classes}")
    if len(symbols_without) > max_symbols_no_label:
        reasons.append(f"symbols_without_labels>{max_symbols_no_label}")
    if len(hints_pending) > max_hints_pending:
        reasons.append(f"hints_pending>{max_hints_pending}")
    if len(extract_missing) > max_missing_extract:
        reasons.append(f"missing_llm_extract>{max_missing_extract}")
    if label_no_features > max_label_no_features:
        reasons.append(f"labels_without_features>{max_label_no_features}")
    if len(sparse) > max_sparse_classes:
        reasons.append(f"sparse_classes>{max_sparse_classes}")
    if features_gap > 0:
        reasons.append(f"features_gap_rows={features_gap}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "n_llm_labels": n_labels,
        "n_trainable_rows": n_trainable,
        "n_classes_distinct": n_classes,
        "labels_by_class": sc.get("labels_by_class"),
        "symbols_without_labels": symbols_without[:12],
        "symbols_with_hints_pending_apply": (sc.get("symbols_with_hints_pending_apply") or [])[:12],
        "hints_pending_apply": hints_pending[:8],
        "events_missing_llm_extract": extract_missing[:8],
        "symbols_needing_llm_extract": (sc.get("symbols_needing_llm_extract") or [])[:12],
        "n_events_missing_llm_extract": len(extract_missing),
        "sparse_classes_below_min_samples": sparse[:8],
        "labels_without_earnings_v1_features": label_no_features,
        "features_gap_rows": features_gap,
    }


def gate_peer_spillover_dataset(data: Dict[str, Any]) -> Dict[str, Any]:
    reasons: list[str] = []
    ps = data.get("peer_spillover_dataset") if isinstance(data.get("peer_spillover_dataset"), dict) else {}
    min_rows = _cfg_int("ML_READINESS_PEER_SPILLOVER_MIN_ROWS", 100)
    min_events = _cfg_int("ML_READINESS_PEER_SPILLOVER_MIN_EVENTS", 10)
    min_trainable = _cfg_int("ML_READINESS_PEER_SPILLOVER_MIN_TRAINABLE_ROWS", 80)
    max_gap_sources = _cfg_int("ML_READINESS_PEER_SPILLOVER_MAX_SOURCES_WITHOUT_ROWS", 8)
    max_cold_peers = _cfg_int("ML_READINESS_PEER_SPILLOVER_MAX_COLD_PEERS", 12)
    max_graph_peers_missing = _cfg_int("ML_READINESS_PEER_SPILLOVER_MAX_GRAPH_PEERS_MISSING", 10)

    n_rows = int(ps.get("n_rows") or 0)
    n_events = int(ps.get("n_events") or 0)
    n_trainable = int(ps.get("n_trainable_rows") or 0)
    gap_sources = ps.get("sources_without_spillover_rows") or []
    cold_peers = ps.get("cold_peers_below_min_samples") or []
    missing_peers = ps.get("peers_in_graph_not_in_train") or []
    features_gap = int(ps.get("features_gap_rows") or 0)

    if n_rows < min_rows:
        reasons.append(f"n_rows<{min_rows}")
    if n_events < min_events:
        reasons.append(f"n_events<{min_events}")
    if n_trainable < min_trainable:
        reasons.append(f"n_trainable_rows<{min_trainable}")
    if len(gap_sources) > max_gap_sources:
        reasons.append(f"sources_without_rows>{max_gap_sources}")
    if len(cold_peers) > max_cold_peers:
        reasons.append(f"cold_peers>{max_cold_peers}")
    if len(missing_peers) > max_graph_peers_missing:
        reasons.append(f"graph_peers_missing>{max_graph_peers_missing}")
    if features_gap > 0:
        reasons.append(f"features_gap_rows={features_gap}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "n_rows": n_rows,
        "n_events": n_events,
        "n_trainable_rows": n_trainable,
        "baseline_weighted_sign_acc": ps.get("baseline_weighted_sign_acc"),
        "sources_without_spillover_rows": gap_sources[:12],
        "cold_peers_below_min_samples": cold_peers[:12],
        "peers_in_graph_not_in_train": missing_peers[:12],
        "features_gap_rows": features_gap,
    }


def gate_peer_spillover_regressor(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not metrics:
        return {"ready": False, "reasons": ["no_peer_spillover_metrics_file"], "rmse_valid": None, "n_train": 0}
    st = metrics.get("status")
    if st != "ok":
        reasons.append(f"status={st}")
    mets = metrics.get("metrics") if isinstance(metrics.get("metrics"), dict) else {}
    n_train = int(mets.get("n_train") or 0)
    min_train = _cfg_int("ML_READINESS_PEER_SPILLOVER_MIN_TRAIN", 80)
    if n_train < min_train:
        reasons.append(f"n_train<{min_train}")
    rmse_max = _cfg_float("ML_READINESS_PEER_SPILLOVER_RMSE_MAX", 0.15)
    rmse = mets.get("rmse_valid")
    if rmse is None or (isinstance(rmse, (int, float)) and float(rmse) > rmse_max):
        reasons.append(f"rmse_valid>{rmse_max}")
    min_sign = _cfg_float("ML_READINESS_PEER_SPILLOVER_MIN_SIGN_ACCURACY", 0.5)
    sign_acc = mets.get("sign_accuracy_valid")
    if sign_acc is not None and float(sign_acc) < min_sign:
        reasons.append(f"sign_accuracy_valid<{min_sign}")
    baseline_sign = mets.get("baseline_sign_accuracy_valid")
    if sign_acc is not None and baseline_sign is not None and float(sign_acc) <= float(baseline_sign):
        reasons.append("sign_acc_not_above_baseline")
    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "rmse_valid": rmse,
        "sign_accuracy_valid": sign_acc,
        "baseline_sign_accuracy_valid": baseline_sign,
        "n_train": n_train,
        "status": st,
    }


def gate_earnings_regression(metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reuse event-reaction regression thresholds for earnings grid layer."""
    reasons: list[str] = []
    if not metrics:
        return {"ready": False, "reasons": ["no_regression_metrics_file"], "rmse_valid": None, "n_train": 0}
    st = metrics.get("status")
    if st != "ok":
        reasons.append(f"status={st}")
    rmse_max = _cfg_float("ML_READINESS_EVENT_REACTION_RMSE_MAX", 0.12)
    mets = metrics.get("metrics") if isinstance(metrics.get("metrics"), dict) else {}
    rmse = mets.get("rmse_valid")
    if rmse is None or (isinstance(rmse, (int, float)) and float(rmse) > rmse_max):
        reasons.append(f"rmse_valid>{rmse_max}")
    nt_min = _cfg_int("ML_READINESS_EVENT_REACTION_MIN_TRAIN", 8)
    nt = int(metrics.get("n_train") or 0)
    if nt < nt_min:
        reasons.append(f"n_train<{nt_min}")
    return {"ready": len(reasons) == 0, "reasons": reasons, "rmse_valid": rmse, "n_train": nt}


def gate_trading_shadow(shadow_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not shadow_report:
        return {"ready": False, "reasons": ["no_shadow_report_file"], "sign_accuracy": None, "n_sign_scored": 0}
    gate = shadow_report.get("trading_gate") if isinstance(shadow_report.get("trading_gate"), dict) else {}
    if gate:
        return {
            "ready": bool(gate.get("ready")),
            "reasons": list(gate.get("reasons") or []),
            "sign_accuracy": (shadow_report.get("aggregate") or {}).get("sign_accuracy"),
            "n_sign_scored": (shadow_report.get("aggregate") or {}).get("n_sign_scored"),
            "mean_pseudo_pnl_log": (shadow_report.get("aggregate") or {}).get("mean_pseudo_pnl_log"),
            "advisory_only": True,
        }
    agg = shadow_report.get("aggregate") if isinstance(shadow_report.get("aggregate"), dict) else {}
    from services.earnings_scenario_shadow import compute_trading_metric_gate

    computed = compute_trading_metric_gate(agg)
    if not computed.get("ready"):
        reasons = list(computed.get("reasons") or [])
    return {
        "ready": bool(computed.get("ready")),
        "reasons": reasons or list(computed.get("reasons") or []),
        "sign_accuracy": agg.get("sign_accuracy"),
        "n_sign_scored": agg.get("n_sign_scored"),
        "mean_pseudo_pnl_log": agg.get("mean_pseudo_pnl_log"),
        "advisory_only": True,
    }


def gate_earnings_autoprep(
    gates: Dict[str, Any],
    snapshot: Dict[str, Any],
    *,
    shadow_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Composite «автоподготовка earnings → product ops»:
    grid + peer spillover + product-tier shadow + минимум LLM labels.
    Все шаги покрыты cron (см. docs/OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md).
    """
    reasons: list[str] = []
    min_labels = _cfg_int("ML_READINESS_EARNINGS_AUTOPREP_MIN_LLM_LABELS", 40)
    min_shadow_matured = _cfg_int("ML_READINESS_EARNINGS_AUTOPREP_MIN_SHADOW_MATURED", 50)
    min_shadow_sign = _cfg_float("ML_READINESS_EARNINGS_AUTOPREP_MIN_SIGN_ACCURACY", 0.58)

    lf = snapshot.get("labels_and_features") if isinstance(snapshot.get("labels_and_features"), dict) else {}
    n_labels = int(lf.get("llm_scenario_labels") or 0)

    grid_ok = bool(gates.get("overall_grid_ready"))
    peer_ok = bool(gates.get("overall_peer_spillover_ready"))
    if not grid_ok:
        reasons.append("overall_grid_ready=false")
    if not peer_ok:
        reasons.append("overall_peer_spillover_ready=false")
    if n_labels < min_labels:
        reasons.append(f"llm_scenario_labels<{min_labels}")

    agg = (shadow_report or {}).get("aggregate") if isinstance(shadow_report, dict) else {}
    if not isinstance(agg, dict):
        agg = {}
    n_matured = int(agg.get("n_matured") or agg.get("n_sign_scored") or 0)
    sign_acc = agg.get("sign_accuracy")
    if n_matured < min_shadow_matured:
        reasons.append(f"shadow_n_matured<{min_shadow_matured}")
    if sign_acc is None:
        reasons.append("shadow_no_sign_accuracy")
    elif float(sign_acc) < min_shadow_sign:
        reasons.append(f"shadow_sign_accuracy<{min_shadow_sign}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "llm_scenario_labels": n_labels,
        "shadow_n_matured": n_matured,
        "shadow_sign_accuracy": sign_acc,
        "overall_grid_ready": grid_ok,
        "overall_peer_spillover_ready": peer_ok,
        "cron_doc": "crontab/lse-docker.crontab + run_earnings_ml_refresh.py",
        "advisory_only": True,
    }


def gate_open_path_mvp_prerequisites(
    snapshot: Dict[str, Any],
    *,
    earnings_autoprep: Dict[str, Any],
) -> Dict[str, Any]:
    """Data + earnings autoprep gates before starting open-path scenario classifier MVP."""
    reasons: list[str] = []
    min_pm_days = _cfg_int("OPEN_PATH_MVP_MIN_PREMARKET_TRADING_DAYS", 60)
    min_gap_open = _cfg_int("OPEN_PATH_MVP_MIN_GAP_FORECAST_OPEN_ROWS", 120)

    od = snapshot.get("open_path_data") if isinstance(snapshot.get("open_path_data"), dict) else {}
    pm_days = int(od.get("premarket_feature_trading_days") or 0)
    gap_open = int(od.get("gap_forecast_open_rows") or 0)

    if not earnings_autoprep.get("ready"):
        reasons.append("earnings_autoprep_not_ready")
    if pm_days < min_pm_days:
        reasons.append(f"premarket_trading_days<{min_pm_days}")
    if gap_open < min_gap_open:
        reasons.append(f"gap_forecast_open_rows<{min_gap_open}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "premarket_feature_trading_days": pm_days,
        "gap_forecast_open_rows": gap_open,
        "earnings_autoprep_ready": bool(earnings_autoprep.get("ready")),
        "next_step_doc": "docs/OPEN_PATH_MVP_AND_EARNINGS_AUTOPREP_PLAN.md §4",
        "advisory_only": True,
    }


def build_earnings_intelligence_gates(
    snapshot: Dict[str, Any],
    *,
    scenario_metrics: Optional[Dict[str, Any]] = None,
    regression_metrics: Optional[Dict[str, Any]] = None,
    peer_spillover_metrics: Optional[Dict[str, Any]] = None,
    shadow_report: Optional[Dict[str, Any]] = None,
    open_path_train_metrics: Optional[Dict[str, Any]] = None,
    open_path_shadow_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    g_sources = gate_sources(snapshot)
    g_features = gate_features(snapshot)
    g_labels = gate_scenario_labels(snapshot)
    g_scenario_ds = gate_scenario_classifier_dataset(snapshot)
    g_scenario = gate_scenario_classifier(scenario_metrics)
    g_regression = gate_earnings_regression(regression_metrics)
    g_peer_ds = gate_peer_spillover_dataset(snapshot)
    g_peer_model = gate_peer_spillover_regressor(peer_spillover_metrics)
    g_shadow = gate_trading_shadow(shadow_report)
    scenario_ready = bool(g_scenario_ds.get("ready")) and bool(g_scenario.get("ready"))
    grid_core = all(g.get("ready") for g in (g_sources, g_features, g_scenario_ds, g_scenario))
    peer_ready = bool(g_peer_ds.get("ready")) and bool(g_peer_model.get("ready"))
    g_autoprep = gate_earnings_autoprep(
        {
            "overall_grid_ready": grid_core,
            "overall_peer_spillover_ready": peer_ready,
        },
        snapshot,
        shadow_report=shadow_report,
    )
    g_open_path = gate_open_path_mvp_prerequisites(snapshot, earnings_autoprep=g_autoprep)
    from services.open_path_readiness import build_open_path_gates

    open_path_gates = build_open_path_gates(
        snapshot,
        train_metrics=open_path_train_metrics,
        shadow_report=open_path_shadow_report,
        prerequisites_ready=bool(g_open_path.get("ready")),
    )
    return {
        "sources": g_sources,
        "features": g_features,
        "scenario_labels": g_labels,
        "scenario_classifier_dataset": g_scenario_ds,
        "scenario_classifier": g_scenario,
        "regression": g_regression,
        "peer_spillover_dataset": g_peer_ds,
        "peer_spillover_regressor": g_peer_model,
        "trading_shadow": g_shadow,
        "earnings_autoprep": g_autoprep,
        "open_path_mvp_prerequisites": g_open_path,
        **open_path_gates,
        "overall_grid_ready": grid_core,
        "overall_scenario_classifier_ready": scenario_ready,
        "overall_with_regression": grid_core and bool(g_regression.get("ready")),
        "overall_trading_shadow_ready": grid_core and bool(g_shadow.get("ready")),
        "overall_peer_spillover_ready": peer_ready,
        "overall_earnings_autoprep_ready": bool(g_autoprep.get("ready")),
        "overall_open_path_mvp_prerequisites_ready": bool(g_open_path.get("ready")),
    }


def write_earnings_intelligence_readiness(
    engine: Engine,
    *,
    project_root: Path | None = None,
    scenario_metrics_path: Path | None = None,
    regression_metrics_path: Path | None = None,
    peer_spillover_metrics_path: Path | None = None,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    feature_builder_version: str = DEFAULT_FEATURE_BUILDER,
) -> Dict[str, Any]:
    root = project_root or Path(__file__).resolve().parents[1]
    snap = collect_earnings_intelligence_readiness(
        engine,
        dataset_version=dataset_version,
        feature_builder_version=feature_builder_version,
    )
    scen_path = scenario_metrics_path or default_scenario_train_metrics_path(root)
    peer_path = peer_spillover_metrics_path or default_peer_spillover_train_metrics_path(root)
    reg_path = regression_metrics_path or (
        Path("/app/logs/ml/ml_data_quality/last_event_reaction_train_metrics.json")
        if Path("/app/logs").exists()
        else root / "local" / "logs" / "ml_data_quality" / "last_event_reaction_train_metrics.json"
    )
    scen_data = _json_load(scen_path)
    reg_data = _json_load(reg_path)
    peer_data = _json_load(peer_path)
    shadow_data = _json_load(default_shadow_report_path(root))
    from services.open_path_readiness import (
        default_shadow_report_path as default_open_path_shadow_path,
        default_train_metrics_path as default_open_path_train_metrics_path,
        write_open_path_readiness,
    )

    open_path_train_data = _json_load(default_open_path_train_metrics_path(root))
    open_path_shadow_data = _json_load(default_open_path_shadow_path(root))
    gates = build_earnings_intelligence_gates(
        snap,
        scenario_metrics=scen_data,
        regression_metrics=reg_data,
        peer_spillover_metrics=peer_data,
        shadow_report=shadow_data,
        open_path_train_metrics=open_path_train_data,
        open_path_shadow_report=open_path_shadow_data,
    )
    bundle = {
        "readiness_version": "earnings_intelligence_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot": snap,
        "gates": gates,
        "metrics_paths": {
            "scenario_classifier": str(scen_path),
            "regression": str(reg_path),
            "peer_spillover_regressor": str(peer_path),
            "peer_spillover_dataset": str(default_peer_spillover_dataset_path(root)),
            "scenario_shadow": str(default_shadow_report_path(root)),
            "open_path_classifier": str(default_open_path_train_metrics_path(root)),
            "open_path_shadow": str(default_open_path_shadow_path(root)),
        },
    }
    try:
        write_open_path_readiness(
            engine,
            project_root=root,
            train_metrics_path=default_open_path_train_metrics_path(root),
            prerequisites_ready=bool(gates.get("overall_open_path_mvp_prerequisites_ready")),
        )
    except Exception as e_opw:
        logger.debug("write_open_path_readiness: %s", e_opw)
    out_path = default_readiness_metrics_path(root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(
        "Wrote earnings intelligence readiness → %s grid=%s scenario=%s peer=%s",
        out_path,
        gates["overall_grid_ready"],
        gates.get("overall_scenario_classifier_ready"),
        gates.get("overall_peer_spillover_ready"),
    )
    return bundle

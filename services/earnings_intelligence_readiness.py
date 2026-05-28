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
    if st not in ("ok", "dry_run"):
        reasons.append(f"status={st}")
    mets = metrics.get("metrics") if isinstance(metrics.get("metrics"), dict) else metrics
    n_train = int(mets.get("n_train") or metrics.get("n_train") or 0)
    min_train = _cfg_int("ML_READINESS_EARNINGS_SCENARIO_MIN_TRAIN", 6)
    if n_train < min_train:
        reasons.append(f"n_train<{min_train}")
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


def build_earnings_intelligence_gates(
    snapshot: Dict[str, Any],
    *,
    scenario_metrics: Optional[Dict[str, Any]] = None,
    regression_metrics: Optional[Dict[str, Any]] = None,
    shadow_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    g_sources = gate_sources(snapshot)
    g_features = gate_features(snapshot)
    g_labels = gate_scenario_labels(snapshot)
    g_scenario = gate_scenario_classifier(scenario_metrics)
    g_regression = gate_earnings_regression(regression_metrics)
    g_shadow = gate_trading_shadow(shadow_report)
    grid_core = all(g.get("ready") for g in (g_sources, g_features, g_labels, g_scenario))
    return {
        "sources": g_sources,
        "features": g_features,
        "scenario_labels": g_labels,
        "scenario_classifier": g_scenario,
        "regression": g_regression,
        "trading_shadow": g_shadow,
        "overall_grid_ready": grid_core,
        "overall_with_regression": grid_core and bool(g_regression.get("ready")),
        "overall_trading_shadow_ready": grid_core and bool(g_shadow.get("ready")),
    }


def write_earnings_intelligence_readiness(
    engine: Engine,
    *,
    project_root: Path | None = None,
    scenario_metrics_path: Path | None = None,
    regression_metrics_path: Path | None = None,
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
    reg_path = regression_metrics_path or (
        Path("/app/logs/ml/ml_data_quality/last_event_reaction_train_metrics.json")
        if Path("/app/logs").exists()
        else root / "local" / "logs" / "ml_data_quality" / "last_event_reaction_train_metrics.json"
    )
    scen_data = _json_load(scen_path)
    reg_data = _json_load(reg_path)
    shadow_data = _json_load(default_shadow_report_path(root))
    gates = build_earnings_intelligence_gates(
        snap,
        scenario_metrics=scen_data,
        regression_metrics=reg_data,
        shadow_report=shadow_data,
    )
    bundle = {
        "readiness_version": "earnings_intelligence_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot": snap,
        "gates": gates,
        "metrics_paths": {
            "scenario_classifier": str(scen_path),
            "regression": str(reg_path),
            "scenario_shadow": str(default_shadow_report_path(root)),
        },
    }
    out_path = default_readiness_metrics_path(root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote earnings intelligence readiness → %s overall_grid_ready=%s", out_path, gates["overall_grid_ready"])
    return bundle

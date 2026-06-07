"""Live shadow report: scenario classifier vs matured forward_log_ret_5d (+ peer spillover)."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.earnings_event_brief import load_peer_edges, load_peer_spillover_outcomes
from services.event_reaction_labeling import FEATURE_BUILDER_VERSION_EARNINGS, timing_from_features_before
from services.earnings_scenario_signal import (
    SCENARIO_SOURCE_SIGN,
    expected_sign_for_scenario,
    predict_scenario_from_features,
)

# Expected peer 5d direction when scenario materializes on spillover names.
SCENARIO_PEER_SIGN: dict[str, float] = {
    "capex_positive_for_infra_peers": 1.0,
    "cross_earnings_contagion": 0.5,
    "gap_up_follow_through": 0.3,
    "miss_or_guide_breakdown": -0.3,
}


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def round_trip_cost_log() -> float:
    """Log-space round-trip cost (buy+sell). Default 20 bps each leg."""
    bps = _cfg_float("PORTFOLIO_ML_TRANSACTION_COST_BPS", 20.0)
    return 2.0 * (bps / 10000.0)


def _json_obj(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def _sign(x: float | None, *, eps: float = 1e-9) -> int:
    if x is None or not math.isfinite(float(x)):
        return 0
    v = float(x)
    if v > eps:
        return 1
    if v < -eps:
        return -1
    return 0


def default_shadow_report_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_earnings_scenario_shadow.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_earnings_scenario_shadow.json"


def _load_matured_rows(
    engine: Engine,
    *,
    dataset_version: str,
    feature_builder_version: str,
    since: str,
) -> list[dict[str, Any]]:
    q = text(
        """
        SELECT
          erd.id,
          erd.symbol,
          erd.knowledge_base_id,
          kb.ts::date AS event_date,
          erd.final_label,
          erd.label_source,
          erd.features_before,
          erd.outcomes_after
        FROM event_reaction_dataset erd
        JOIN knowledge_base kb ON kb.id = erd.knowledge_base_id
        WHERE erd.dataset_version = :dv
          AND kb.ts::date >= CAST(:since AS date)
          AND kb.ts::date <= CURRENT_DATE
          AND erd.features_before IS NOT NULL
          AND erd.features_before <> '{}'::jsonb
          AND (erd.features_before->>'feature_builder_version') = :fbv
          AND erd.outcomes_after ? 'forward_log_ret_5d'
        ORDER BY kb.ts DESC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            q,
            {"dv": dataset_version, "fbv": feature_builder_version, "since": since},
        ).mappings().all()
    return [dict(r) for r in rows]


def _peer_mean_5d(
    engine: Engine,
    *,
    source_symbol: str,
    event_date: date,
    source_market_phase: str = "UNKNOWN",
) -> dict[str, Any]:
    peers = load_peer_edges(engine, source_ticker=source_symbol)
    targets = [str(p.get("target_ticker") or "").upper() for p in peers if p.get("target_ticker")]
    if not targets:
        return {"n_peers": 0, "mean_forward_log_ret_5d": None, "status": "no_peers"}
    spill = load_peer_spillover_outcomes(
        source_event_date=event_date,
        peer_tickers=targets[:12],
        source_market_phase=source_market_phase,
    )
    vals = [
        float(p["forward_log_ret_5d"])
        for p in spill
        if p.get("status") == "ok" and p.get("forward_log_ret_5d") is not None
    ]
    if not vals:
        return {"n_peers": len(targets), "mean_forward_log_ret_5d": None, "status": "no_peer_outcomes"}
    return {
        "n_peers": len(targets),
        "n_with_5d": len(vals),
        "mean_forward_log_ret_5d": round(sum(vals) / len(vals), 6),
        "status": "ok",
    }


def evaluate_earnings_scenario_shadow(
    engine: Engine,
    *,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: str = "2026-01-01",
) -> dict[str, Any]:
    rows = _load_matured_rows(
        engine,
        dataset_version=dataset_version,
        feature_builder_version=feature_builder_version,
        since=since,
    )
    rt_cost = round_trip_cost_log()
    threshold_log = _cfg_float("EVENT_REACTION_LABEL_THRESHOLD_LOG", 0.004)

    detail: list[dict[str, Any]] = []
    sign_hits = 0
    sign_total = 0
    class_hits = 0
    class_total = 0
    peer_sign_hits = 0
    peer_sign_total = 0
    pseudo_pnls: list[float] = []

    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        ev_d = r.get("event_date")
        if isinstance(ev_d, str):
            ev_d = date.fromisoformat(ev_d[:10])
        outcomes = _json_obj(r.get("outcomes_after"))
        actual_5d = outcomes.get("forward_log_ret_5d")
        try:
            actual_5d_f = float(actual_5d) if actual_5d is not None else None
        except (TypeError, ValueError):
            actual_5d_f = None

        pred = predict_scenario_from_features(sym, r.get("features_before"), feature_builder_version=feature_builder_version)
        pred_scenario = pred.get("predicted_scenario")
        exp_sign = pred.get("predicted_scenario_sign")
        if exp_sign is None and pred_scenario:
            exp_sign = expected_sign_for_scenario(str(pred_scenario))

        actual_label = str(r.get("final_label") or "").strip() or None
        label_source = str(r.get("label_source") or "")

        sign_match: bool | None = None
        pseudo_pnl: float | None = None
        if actual_5d_f is not None and exp_sign is not None and float(exp_sign) != 0.0:
            act_sign = _sign(actual_5d_f, eps=threshold_log)
            exp_s = _sign(float(exp_sign), eps=0.05)
            if exp_s != 0 and act_sign != 0:
                sign_match = act_sign == exp_s
                sign_total += 1
                if sign_match:
                    sign_hits += 1
                direction = 1.0 if exp_s > 0 else -1.0
                pseudo_pnl = direction * actual_5d_f - rt_cost
                pseudo_pnls.append(pseudo_pnl)

        class_match: bool | None = None
        if pred_scenario and actual_label and label_source == "llm_scenario_v0":
            class_match = str(pred_scenario) == actual_label
            class_total += 1
            if class_match:
                class_hits += 1

        peer_block = _peer_mean_5d(
            engine,
            source_symbol=sym,
            event_date=ev_d,
            source_market_phase=timing_from_features_before(r.get("features_before")),
        ) if ev_d else {}
        peer_mean = peer_block.get("mean_forward_log_ret_5d")
        peer_exp = SCENARIO_PEER_SIGN.get(str(pred_scenario or ""))
        peer_sign_match: bool | None = None
        if peer_mean is not None and peer_exp is not None and float(peer_exp) != 0.0:
            p_exp = _sign(float(peer_exp), eps=0.05)
            p_act = _sign(float(peer_mean), eps=threshold_log)
            if p_exp != 0 and p_act != 0:
                peer_sign_match = p_exp == p_act
                peer_sign_total += 1
                if peer_sign_match:
                    peer_sign_hits += 1

        detail.append(
            {
                "symbol": sym,
                "event_date": ev_d.isoformat() if ev_d else None,
                "actual_forward_log_ret_5d": actual_5d_f,
                "actual_scenario_label": actual_label,
                "label_source": label_source,
                "predicted_scenario": pred_scenario,
                "predicted_scenario_proba": pred.get("predicted_scenario_proba"),
                "predicted_scenario_sign": exp_sign,
                "scenario_classifier_status": pred.get("scenario_classifier_status"),
                "sign_match": sign_match,
                "class_match": class_match,
                "pseudo_pnl_log": round(pseudo_pnl, 6) if pseudo_pnl is not None else None,
                "peer_spillover": peer_block,
                "peer_sign_match": peer_sign_match,
            }
        )

    agg = {
        "n_matured": len(rows),
        "n_sign_scored": sign_total,
        "sign_accuracy": round(sign_hits / sign_total, 4) if sign_total else None,
        "n_class_scored": class_total,
        "class_accuracy": round(class_hits / class_total, 4) if class_total else None,
        "n_peer_sign_scored": peer_sign_total,
        "peer_sign_accuracy": round(peer_sign_hits / peer_sign_total, 4) if peer_sign_total else None,
        "mean_pseudo_pnl_log": round(sum(pseudo_pnls) / len(pseudo_pnls), 6) if pseudo_pnls else None,
        "sum_pseudo_pnl_log": round(sum(pseudo_pnls), 6) if pseudo_pnls else None,
        "round_trip_cost_log": round(rt_cost, 6),
        "transaction_cost_bps_per_leg": _cfg_float("PORTFOLIO_ML_TRANSACTION_COST_BPS", 20.0),
    }
    trading_gate = compute_trading_metric_gate(agg)

    return {
        "report_version": "earnings_scenario_shadow_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset_version,
        "feature_builder_version": feature_builder_version,
        "since": since,
        "scenario_source_sign_map": SCENARIO_SOURCE_SIGN,
        "scenario_peer_sign_map": SCENARIO_PEER_SIGN,
        "aggregate": agg,
        "trading_gate": trading_gate,
        "rows": detail,
    }


def compute_trading_metric_gate(aggregate: dict[str, Any]) -> dict[str, Any]:
    """Gate for trading-facing fusion (advisory shadow only)."""
    reasons: list[str] = []
    min_n = _cfg_int("ML_READINESS_EARNINGS_SHADOW_MIN_MATURED", 6)
    min_sign_acc = _cfg_float("ML_READINESS_EARNINGS_SHADOW_MIN_SIGN_ACCURACY", 0.55)
    min_mean_pnl = _cfg_float("ML_READINESS_EARNINGS_SHADOW_MIN_MEAN_PNL_LOG", 0.0)

    n = int(aggregate.get("n_sign_scored") or 0)
    sign_acc = aggregate.get("sign_accuracy")
    mean_pnl = aggregate.get("mean_pseudo_pnl_log")

    if n < min_n:
        reasons.append(f"n_sign_scored<{min_n}")
    if sign_acc is None:
        reasons.append("no_sign_accuracy")
    elif float(sign_acc) < min_sign_acc:
        reasons.append(f"sign_accuracy<{min_sign_acc}")
    if mean_pnl is None:
        reasons.append("no_mean_pseudo_pnl")
    elif float(mean_pnl) < min_mean_pnl:
        reasons.append(f"mean_pseudo_pnl_log<{min_mean_pnl}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "n_sign_scored": n,
        "sign_accuracy": sign_acc,
        "mean_pseudo_pnl_log": mean_pnl,
        "advisory_only": True,
        "note": "Shadow gate does not enable live trading; fusion remains advisory until backtest + manual approval.",
    }


def write_earnings_scenario_shadow_report(
    engine: Engine,
    *,
    project_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    report = evaluate_earnings_scenario_shadow(engine, **kwargs)
    out_path = default_shadow_report_path(project_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report

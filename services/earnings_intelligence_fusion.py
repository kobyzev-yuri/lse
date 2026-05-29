"""Advisory fusion: regression forward 5d + scenario classifier + event brief."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from sqlalchemy.engine import Engine

from services.earnings_event_brief import build_event_brief, load_event_brief_inputs
from services.earnings_scenario_signal import predict_scenario_from_features
from services.event_reaction_catboost_signal import predict_event_reaction_for_ticker
from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_EARNINGS,
    compute_row_labeling,
)


def _advisory_stance(
    *,
    reg_pred: float | None,
    scenario: str | None,
    scenario_sign: float | None,
    threshold_log: float = 0.004,
) -> dict[str, Any]:
    """Human-readable advisory bundle (not an execution signal)."""
    notes: list[str] = []
    reg_bias: str | None = None
    if reg_pred is not None:
        if reg_pred > threshold_log:
            reg_bias = "bullish_5d"
        elif reg_pred < -threshold_log:
            reg_bias = "bearish_5d"
        else:
            reg_bias = "neutral_5d"

    scen_bias: str | None = None
    if scenario_sign is not None:
        if float(scenario_sign) > 0.2:
            scen_bias = "scenario_bullish"
        elif float(scenario_sign) < -0.2:
            scen_bias = "scenario_bearish"
        else:
            scen_bias = "scenario_mixed"

    alignment = "unknown"
    if reg_bias and scen_bias:
        if reg_bias == scen_bias or (reg_bias.startswith("neutral") or scen_bias == "scenario_mixed"):
            alignment = "aligned_or_weak"
        elif (reg_bias == "bullish_5d" and scen_bias == "scenario_bearish") or (
            reg_bias == "bearish_5d" and scen_bias == "scenario_bullish"
        ):
            alignment = "conflict"
            notes.append("Regression and scenario classifier disagree — treat as low conviction.")
        else:
            alignment = "partial"

    conviction = "low"
    if alignment == "aligned_or_weak" and reg_pred is not None and abs(reg_pred) > threshold_log * 2:
        conviction = "medium"
    if alignment == "conflict":
        conviction = "low"

    summary_parts = []
    if reg_pred is not None:
        summary_parts.append(f"regression 5d log-ret {reg_pred:+.4f}")
    if scenario:
        summary_parts.append(f"scenario {scenario}")
    summary = " · ".join(summary_parts) if summary_parts else "insufficient ML inputs"

    return {
        "regression_bias": reg_bias,
        "scenario_bias": scen_bias,
        "alignment": alignment,
        "conviction": conviction,
        "summary": summary,
        "notes": notes,
        "advisory_only": True,
        "execution_blocked": True,
    }


def build_earnings_fusion_advisory(
    engine: Engine,
    *,
    symbol: str,
    event_date: date,
    dataset_version: str = "v0_expanded_baseline",
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    row = load_event_brief_inputs(engine, symbol=sym, event_date=event_date, dataset_version=dataset_version)
    if not row or not row.get("knowledge_base_id"):
        return {
            "status": "not_found",
            "symbol": sym,
            "event_date": event_date.isoformat(),
            "reason": "no knowledge_base EARNINGS row for symbol/date",
        }

    brief = build_event_brief(engine, symbol=sym, event_date=event_date, dataset_version=dataset_version)
    features = row.get("features_before")
    if isinstance(features, str):
        import json

        try:
            features = json.loads(features)
        except Exception:
            features = None
    fb = ""
    if isinstance(features, dict):
        fb = str(features.get("feature_builder_version") or "")

    reg_out = predict_event_reaction_for_ticker(sym, event_date=event_date)

    scen_feats = features if fb == FEATURE_BUILDER_VERSION_EARNINGS else None
    if scen_feats is None:
        rebuilt, _, _, _ = compute_row_labeling(
            sym,
            event_date,
            knowledge_base_id=row.get("knowledge_base_id"),
            feature_builder_version=FEATURE_BUILDER_VERSION_EARNINGS,
        )
        scen_feats = rebuilt
    scen_out = predict_scenario_from_features(
        sym,
        scen_feats,
        feature_builder_version=FEATURE_BUILDER_VERSION_EARNINGS,
    )

    reg_pred = reg_out.get("event_reaction_ml_forward_log_ret_5d_pred")
    try:
        reg_pred_f = float(reg_pred) if reg_pred is not None else None
    except (TypeError, ValueError):
        reg_pred_f = None

    scen_sign = scen_out.get("predicted_scenario_sign")
    advisory = _advisory_stance(
        reg_pred=reg_pred_f,
        scenario=scen_out.get("predicted_scenario"),
        scenario_sign=float(scen_sign) if scen_sign is not None else None,
    )

    llm_scenario = (brief.get("scenario") or {}).get("id")
    return {
        "status": "ok",
        "symbol": sym,
        "event_date": event_date.isoformat(),
        "fusion_version": "earnings_advisory_v1",
        "brief_headline": {
            "management_tone": brief.get("management_tone"),
            "llm_scenario": llm_scenario,
            "source_outcomes": brief.get("source_outcomes"),
        },
        "regression_ml": {
            "status": reg_out.get("event_reaction_ml_status"),
            "forward_log_ret_5d_pred": reg_pred_f,
            "feature_builder": reg_out.get("event_reaction_ml_feature_builder_version"),
        },
        "scenario_ml": scen_out,
        "peer_spillover": brief.get("peer_spillover_outcomes"),
        "advisory": advisory,
    }

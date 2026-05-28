"""Assemble Earnings Event Brief JSON from DB facts (extraction + outcomes + peer graph)."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


def _parse_json(val: Any) -> dict | list | None:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return None
    return None


def _top_scenario(guidance_summary: dict | None) -> dict | None:
    if not guidance_summary:
        return None
    hints = guidance_summary.get("scenario_hints")
    if not isinstance(hints, list) or not hints:
        return None
    ranked = sorted(
        [h for h in hints if isinstance(h, dict) and h.get("scenario")],
        key=lambda h: {"high": 0, "medium": 1, "low": 2}.get(str(h.get("confidence") or "").lower(), 3),
    )
    return ranked[0] if ranked else None


def load_event_brief_inputs(
    engine: Engine,
    *,
    symbol: str,
    event_date: date,
    dataset_version: str = "v0_expanded_baseline",
) -> dict[str, Any] | None:
    sym = symbol.strip().upper()
    q = text(
        """
        WITH kb AS (
          SELECT id, ticker, ts::date AS event_date, content
          FROM knowledge_base
          WHERE UPPER(TRIM(ticker)) = :symbol
            AND ts::date = :event_date
            AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
          ORDER BY id DESC
          LIMIT 1
        )
        SELECT
          kb.id AS knowledge_base_id,
          kb.ticker,
          kb.event_date,
          ed.fiscal_period,
          ed.revenue_actual,
          ed.revenue_estimate,
          ed.eps_actual,
          ed.eps_estimate,
          ed.guidance_summary,
          ed.affected_tickers,
          erd.id AS dataset_row_id,
          erd.final_label,
          erd.label_source,
          erd.features_before,
          erd.outcomes_after,
          erd.ticker_price_regime
        FROM kb
        LEFT JOIN earnings_event_detail ed ON ed.knowledge_base_id = kb.id
        LEFT JOIN event_reaction_dataset erd
          ON erd.knowledge_base_id = kb.id
         AND erd.dataset_version = :dataset_version
        """
    )
    with engine.connect() as conn:
        row = conn.execute(
            q,
            {"symbol": sym, "event_date": event_date, "dataset_version": dataset_version},
        ).mappings().first()
    return dict(row) if row else None


def load_peer_edges(engine: Engine, *, source_ticker: str) -> list[dict[str, Any]]:
    q = text(
        """
        SELECT target_ticker, relation_type, weight, meta
        FROM peer_graph_edge
        WHERE UPPER(source_ticker) = :source
        ORDER BY weight DESC, target_ticker
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, {"source": source_ticker.strip().upper()}).mappings().all()
    return [dict(r) for r in rows]


def build_event_brief(
    engine: Engine,
    *,
    symbol: str,
    event_date: date,
    dataset_version: str = "v0_expanded_baseline",
) -> dict[str, Any]:
    row = load_event_brief_inputs(engine, symbol=symbol, event_date=event_date, dataset_version=dataset_version)
    if not row or not row.get("knowledge_base_id"):
        return {
            "status": "not_found",
            "symbol": symbol.upper(),
            "event_date": event_date.isoformat(),
            "reason": "no knowledge_base EARNINGS row for symbol/date",
        }

    guidance = _parse_json(row.get("guidance_summary"))
    guidance = guidance if isinstance(guidance, dict) else {}
    affected = _parse_json(row.get("affected_tickers"))
    affected = affected if isinstance(affected, list) else []
    outcomes = _parse_json(row.get("outcomes_after"))
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    features = _parse_json(row.get("features_before"))
    features = features if isinstance(features, dict) else {}

    peers = load_peer_edges(engine, source_ticker=symbol)
    scenario = _top_scenario(guidance)

    evidence = guidance.get("evidence_quotes") or []
    if not isinstance(evidence, list):
        evidence = []

    brief: dict[str, Any] = {
        "status": "ok",
        "symbol": symbol.upper(),
        "event_date": event_date.isoformat(),
        "fiscal_period": row.get("fiscal_period"),
        "knowledge_base_id": row.get("knowledge_base_id"),
        "headline": None,
        "scenario": {
            "id": scenario.get("scenario") if scenario else row.get("final_label"),
            "confidence": scenario.get("confidence") if scenario else None,
            "rationale": scenario.get("rationale") if scenario else None,
            "source": "llm_scenario_hints" if scenario else row.get("label_source"),
        },
        "management_tone": guidance.get("management_tone"),
        "guidance": guidance.get("guidance"),
        "capex_notes": guidance.get("capex_notes"),
        "affected_tickers": affected,
        "peer_graph": peers,
        "evidence_quotes": evidence[:5],
        "source_outcomes": {
            "forward_log_ret_1d": outcomes.get("forward_log_ret_1d"),
            "forward_log_ret_2d": outcomes.get("forward_log_ret_2d"),
            "forward_log_ret_5d": outcomes.get("forward_log_ret_5d"),
            "forward_log_ret_20d": outcomes.get("forward_log_ret_20d"),
            "final_label_auto": row.get("final_label"),
        },
        "source_features_snapshot": {
            "feature_builder_version": features.get("feature_builder_version"),
            "ticker_price_regime": row.get("ticker_price_regime"),
        },
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
    }

    scen_id = brief["scenario"].get("id")
    if scen_id == "capex_positive_for_infra_peers":
        brief["headline"] = f"{symbol.upper()} capex / AI infra signal — watch peer spillover"
    elif scen_id == "gap_up_follow_through":
        brief["headline"] = f"{symbol.upper()} earnings — gap-up follow-through scenario"
    elif scen_id:
        brief["headline"] = f"{symbol.upper()} earnings — scenario {scen_id}"
    else:
        brief["headline"] = f"{symbol.upper()} earnings event brief"

    if not guidance.get("management_tone") and not scenario:
        brief["status"] = "partial"
        brief["reason"] = "earnings_event_detail missing LLM extraction; run extract_earnings_material_facts.py"

    return brief

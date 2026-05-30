"""Assemble Earnings Event Brief JSON from DB facts (extraction + outcomes + peer graph)."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.event_reaction_labeling import (
    _find_as_of_index,
    _log_ret_ratio,
    build_outcomes_after,
    load_quotes_window,
)
from services.earnings_scenario_signal import predict_scenario_from_features


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


def _normalize_evidence_quotes(raw: Any) -> list[dict[str, str]]:
    """LLM stores {topic, quote}; legacy rows may be plain strings."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:5]:
        if isinstance(item, str):
            q = item.strip()
            if q:
                out.append({"topic": "other", "quote": q})
            continue
        if not isinstance(item, dict):
            continue
        q = str(item.get("quote") or item.get("text") or item.get("content") or "").strip()
        if not q:
            continue
        out.append(
            {
                "topic": str(item.get("topic") or "other").strip() or "other",
                "quote": q,
            }
        )
    return out


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
    if not row:
        return None
    item = dict(row)
    for key in ("revenue_actual", "revenue_estimate", "eps_actual", "eps_estimate"):
        val = item.get(key)
        if val is not None:
            item[key] = float(val)
    return item


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
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        w = item.get("weight")
        if w is not None:
            item["weight"] = float(w)
        out.append(item)
    return out


def _peer_tickers_for_brief(
    *,
    peers: list[dict[str, Any]],
    affected: list[Any],
    max_peers: int = 12,
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for p in peers:
        t = str(p.get("target_ticker") or "").strip().upper()
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    for item in affected:
        if isinstance(item, str):
            t = item.strip().upper()
        elif isinstance(item, dict):
            t = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
        else:
            continue
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered[:max_peers]


def load_peer_spillover_outcomes(
    *,
    source_event_date: date,
    peer_tickers: list[str],
    horizons: tuple[int, ...] = (1, 2, 5),
) -> list[dict[str, Any]]:
    """Forward log-returns for peer tickers anchored at source earnings event date."""
    d_min = source_event_date - timedelta(days=10)
    d_max = source_event_date + timedelta(days=60)
    out: list[dict[str, Any]] = []
    for sym in peer_tickers:
        sym_u = sym.strip().upper()
        if not sym_u:
            continue
        df = load_quotes_window(sym_u, date_min=d_min, date_max=d_max)
        if df.empty:
            out.append({"ticker": sym_u, "status": "no_quotes"})
            continue
        dates = list(df["d"])
        as_of_i = _find_as_of_index(dates, source_event_date)
        if as_of_i is None:
            out.append({"ticker": sym_u, "status": "no_as_of_before_event"})
            continue
        outcomes, _ = build_outcomes_after(df, as_of_idx=as_of_i, horizons=horizons)
        row: dict[str, Any] = {
            "ticker": sym_u,
            "status": "ok",
            "anchor_date": str(dates[as_of_i]),
        }
        for h in horizons:
            key = f"forward_log_ret_{h}d"
            val = outcomes.get(key)
            if val is not None and math.isfinite(float(val)):
                row[key] = float(val)
        if len(row) <= 3:
            row["status"] = "insufficient_forward"
        out.append(row)
    return out


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
    peer_targets = _peer_tickers_for_brief(peers=peers, affected=affected)
    peer_outcomes = load_peer_spillover_outcomes(
        source_event_date=event_date,
        peer_tickers=peer_targets,
    )
    scenario = _top_scenario(guidance)

    evidence = _normalize_evidence_quotes(guidance.get("evidence_quotes"))
    from services.peer_spillover_signal import predict_peer_spillover_batch

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
        "peer_spillover_outcomes": peer_outcomes,
        "peer_spillover_ml": predict_peer_spillover_batch(
            source_symbol=symbol,
            features_before=features,
            peer_edges=peers,
        ),
        "evidence_quotes": evidence,
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
        "scenario_ml": predict_scenario_from_features(symbol, features),
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

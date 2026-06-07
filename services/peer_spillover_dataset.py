"""Build (source_event, peer) rows for peer spillover ML — Phase C."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_EARNINGS,
    FEATURE_BUILDER_VERSION_QUOTES,
    event_reaction_numeric_feature_keys,
    timing_from_features_before,
)


def _parse_json(val: Any) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            o = json.loads(val)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def build_peer_spillover_dataset_rows(
    engine: Engine,
    *,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: date | str = "2026-01-01",
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """
    One row per (source earnings event, peer_ticker) with matured peer 5d outcome.

    y: peer_forward_log_ret_5d
    Features (MVP): edge weight, relation_type, source_forward_log_ret_5d, scenario sign hints.
    """
    from services.earnings_event_brief import load_peer_edges, load_peer_spillover_outcomes

    q = text(
        """
        SELECT
          erd.id AS dataset_id,
          erd.symbol AS source_symbol,
          erd.knowledge_base_id,
          kb.ts::date AS event_date,
          erd.features_before,
          erd.outcomes_after
        FROM event_reaction_dataset erd
        JOIN knowledge_base kb ON kb.id = erd.knowledge_base_id
        WHERE erd.dataset_version = :dv
          AND kb.ts::date >= CAST(:since AS date)
          AND erd.features_before IS NOT NULL
          AND erd.features_before <> '{}'::jsonb
          AND (erd.features_before->>'feature_builder_version') = :fbv
          AND erd.outcomes_after ? 'forward_log_ret_5d'
        ORDER BY kb.ts DESC
        LIMIT :lim
        """
    )
    params = {
        "dv": dataset_version,
        "fbv": feature_builder_version,
        "since": str(since)[:10],
        "lim": max(1, int(limit)),
    }
    rows_out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        events = conn.execute(q, params).mappings().all()

    for ev in events:
        source = str(ev["source_symbol"]).upper()
        event_date = ev["event_date"]
        if not isinstance(event_date, date):
            continue
        outcomes = _parse_json(ev.get("outcomes_after"))
        src_5d = outcomes.get("forward_log_ret_5d")
        try:
            src_5d_f = float(src_5d) if src_5d is not None else None
        except (TypeError, ValueError):
            src_5d_f = None
        if src_5d_f is None or not math.isfinite(src_5d_f):
            continue

        peers = load_peer_edges(engine, source_ticker=source)
        if not peers:
            continue
        targets = [
            str(p.get("target_ticker") or "").upper()
            for p in peers
            if p.get("target_ticker") and str(p.get("relation_type") or "") != "sector_etf"
        ]
        if not targets:
            continue
        spill = load_peer_spillover_outcomes(
            source_event_date=event_date,
            peer_tickers=targets,
            source_market_phase=timing_from_features_before(ev.get("features_before")),
        )
        spill_by = {str(p.get("ticker") or "").upper(): p for p in spill if p.get("status") == "ok"}

        for edge in peers:
            peer = str(edge.get("target_ticker") or "").upper()
            if not peer or edge.get("relation_type") == "sector_etf":
                continue
            po = spill_by.get(peer)
            if not po:
                continue
            peer_5d = po.get("forward_log_ret_5d")
            try:
                peer_5d_f = float(peer_5d)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(peer_5d_f):
                continue
            weight = float(edge.get("weight") or 0.5)
            rows_out.append(
                {
                    "dataset_id": int(ev["dataset_id"]),
                    "source_symbol": source,
                    "peer_ticker": peer,
                    "event_date": event_date.isoformat(),
                    "knowledge_base_id": ev.get("knowledge_base_id"),
                    "relation_type": edge.get("relation_type"),
                    "edge_weight": weight,
                    "source_market_phase": timing_from_features_before(ev.get("features_before")),
                    "source_forward_log_ret_5d": round(src_5d_f, 6),
                    "peer_forward_log_ret_5d": round(peer_5d_f, 6),
                    "baseline_propagation_log": round(weight * src_5d_f, 6),
                }
            )
    return rows_out


def peer_spillover_categorical_features() -> tuple[str, ...]:
    return ("source_symbol", "peer_ticker", "relation_type", "source_market_phase")


def peer_spillover_numeric_edge_features() -> tuple[str, ...]:
    return ("edge_weight",)


def peer_spillover_feature_names(
    *,
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
) -> list[str]:
    src_keys = list(event_reaction_numeric_feature_keys(feature_builder_version))
    return list(peer_spillover_numeric_edge_features()) + src_keys + list(peer_spillover_categorical_features())


def load_peer_spillover_training_frame(
    engine: Engine,
    *,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: date | str = "2026-01-01",
    limit: int = 2000,
) -> pd.DataFrame:
    """Training frame: pre-event source features + edge metadata → peer_forward_log_ret_5d."""
    numeric_keys = event_reaction_numeric_feature_keys(feature_builder_version)
    quote_keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_QUOTES)
    extra_keys = tuple(k for k in numeric_keys if k not in quote_keys)
    rows_raw = build_peer_spillover_dataset_rows(
        engine,
        dataset_version=dataset_version,
        feature_builder_version=feature_builder_version,
        since=since,
        limit=limit,
    )
    if not rows_raw:
        return pd.DataFrame()

    ids = sorted({int(r["dataset_id"]) for r in rows_raw})
    q = text(
        """
        SELECT id, features_before
        FROM event_reaction_dataset
        WHERE id = ANY(:ids)
        """
    )
    fb_by_id: dict[int, dict[str, Any]] = {}
    with engine.connect() as conn:
        for row in conn.execute(q, {"ids": ids}).mappings():
            fb_by_id[int(row["id"])] = _parse_json(row.get("features_before"))

    out_rows: list[dict[str, Any]] = []
    for r in rows_raw:
        fb = fb_by_id.get(int(r["dataset_id"]), {})
        if str(fb.get("feature_builder_version") or "") != feature_builder_version:
            continue
        rec: dict[str, Any] = {
            "dataset_id": int(r["dataset_id"]),
            "event_date": r["event_date"],
            "target_peer_forward_log_ret_5d": float(r["peer_forward_log_ret_5d"]),
            "baseline_propagation_log": float(r["baseline_propagation_log"]),
            "source_forward_log_ret_5d": float(r["source_forward_log_ret_5d"]),
            "source_symbol": str(r["source_symbol"]).upper(),
            "peer_ticker": str(r["peer_ticker"]).upper(),
            "relation_type": str(r.get("relation_type") or "unknown"),
            "source_market_phase": str(r.get("source_market_phase") or "UNKNOWN").upper(),
            "edge_weight": float(r.get("edge_weight") or 0.5),
        }
        skip = False
        for k in quote_keys:
            try:
                fv = float(fb.get(k))
            except (TypeError, ValueError):
                skip = True
                break
            if not math.isfinite(fv):
                skip = True
                break
            rec[k] = fv
        if skip:
            continue
        for k in extra_keys:
            try:
                fv = float(fb.get(k)) if fb.get(k) is not None else 0.0
            except (TypeError, ValueError):
                fv = 0.0
            if not math.isfinite(fv):
                fv = 0.0
            rec[k] = fv
        out_rows.append(rec)
    if not out_rows:
        return pd.DataFrame()
    frame = pd.DataFrame(out_rows)
    return frame.sort_values(["event_date", "dataset_id", "peer_ticker"]).reset_index(drop=True)


def summarize_peer_spillover_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n_rows": 0, "n_events": 0, "n_peers": 0}
    events = {(r["source_symbol"], r["event_date"]) for r in rows}
    peers = {r["peer_ticker"] for r in rows}
    signs = sum(
        1
        for r in rows
        if r.get("peer_forward_log_ret_5d") is not None
        and r.get("source_forward_log_ret_5d") is not None
        and (r["peer_forward_log_ret_5d"] >= 0) == (r["source_forward_log_ret_5d"] >= 0)
    )
    baseline_hits = sum(
        1
        for r in rows
        if r.get("peer_forward_log_ret_5d") is not None
        and r.get("baseline_propagation_log") is not None
        and (r["peer_forward_log_ret_5d"] >= 0) == (r["baseline_propagation_log"] >= 0)
    )
    return {
        "n_rows": len(rows),
        "n_events": len(events),
        "n_peers_distinct": len(peers),
        "same_sign_rate": round(signs / len(rows), 4) if rows else None,
        "baseline_weighted_sign_acc": round(baseline_hits / len(rows), 4) if rows else None,
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
    }


def collect_peer_spillover_coverage(
    engine: Engine,
    *,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: date | str = "2026-01-01",
    limit: int = 2000,
    universe: list[str] | None = None,
    min_peer_samples: int = 2,
) -> dict[str, Any]:
    """
    Dataset quality snapshot for analyzer / readiness gates.

    Highlights training gaps: sources with matured events but no peer rows,
    graph peers never seen in y, cold peers with too few samples.
    """
    from services.earnings_intelligence_universe import get_earnings_intelligence_universe

    sym_set = sorted({s.strip().upper() for s in (universe or get_earnings_intelligence_universe()) if s.strip()})
    raw_rows = build_peer_spillover_dataset_rows(
        engine,
        dataset_version=dataset_version,
        feature_builder_version=feature_builder_version,
        since=since,
        limit=limit,
    )
    train_frame = load_peer_spillover_training_frame(
        engine,
        dataset_version=dataset_version,
        feature_builder_version=feature_builder_version,
        since=since,
        limit=limit,
    )
    summary = summarize_peer_spillover_rows(raw_rows)

    rows_by_source: dict[str, int] = {}
    rows_by_peer: dict[str, int] = {}
    sources_with_rows: set[str] = set()
    peers_with_rows: set[str] = set()
    for r in raw_rows:
        src = str(r["source_symbol"]).upper()
        peer = str(r["peer_ticker"]).upper()
        rows_by_source[src] = rows_by_source.get(src, 0) + 1
        rows_by_peer[peer] = rows_by_peer.get(peer, 0) + 1
        sources_with_rows.add(src)
        peers_with_rows.add(peer)

    matured_sources: set[str] = set()
    graph_sources: set[str] = set()
    graph_peers: set[str] = set()
    try:
        with engine.connect() as conn:
            matured_q = text(
                """
                SELECT DISTINCT UPPER(TRIM(erd.symbol)) AS source_symbol
                FROM event_reaction_dataset erd
                JOIN knowledge_base kb ON kb.id = erd.knowledge_base_id
                WHERE erd.dataset_version = :dv
                  AND kb.ts::date >= CAST(:since AS date)
                  AND UPPER(TRIM(erd.symbol)) = ANY(:symbols)
                  AND erd.features_before IS NOT NULL
                  AND erd.features_before <> '{}'::jsonb
                  AND (erd.features_before->>'feature_builder_version') = :fbv
                  AND erd.outcomes_after ? 'forward_log_ret_5d'
                  AND EXISTS (
                    SELECT 1 FROM peer_graph_edge pge
                    WHERE UPPER(TRIM(pge.source_ticker)) = UPPER(TRIM(erd.symbol))
                      AND COALESCE(pge.relation_type, '') <> 'sector_etf'
                  )
                """
            )
            matured_sources = {
                str(r["source_symbol"]).upper()
                for r in conn.execute(
                    matured_q,
                    {"dv": dataset_version, "fbv": feature_builder_version, "since": str(since)[:10], "symbols": sym_set},
                ).mappings()
            }
            graph_sources = {
                str(r[0]).upper()
                for r in conn.execute(
                    text(
                        """
                        SELECT DISTINCT UPPER(TRIM(source_ticker))
                        FROM peer_graph_edge
                        WHERE COALESCE(relation_type, '') <> 'sector_etf'
                          AND UPPER(TRIM(source_ticker)) = ANY(:symbols)
                        """
                    ),
                    {"symbols": sym_set},
                ).all()
            }
            graph_peers = {
                str(r[0]).upper()
                for r in conn.execute(
                    text(
                        """
                        SELECT DISTINCT UPPER(TRIM(target_ticker))
                        FROM peer_graph_edge
                        WHERE COALESCE(relation_type, '') <> 'sector_etf'
                          AND UPPER(TRIM(target_ticker)) = ANY(:symbols)
                        """
                    ),
                    {"symbols": sym_set},
                ).all()
            }
    except Exception as e:
        summary["coverage_error"] = str(e)

    sources_without_rows = sorted(matured_sources - sources_with_rows)
    graph_sources_without_rows = sorted(graph_sources - sources_with_rows)
    peers_in_graph_not_in_train = sorted(graph_peers - peers_with_rows)
    cold_peers = sorted(p for p, n in rows_by_peer.items() if n < max(1, int(min_peer_samples)))
    universe_sources_never_leader = sorted(s for s in sym_set if s in graph_peers and s not in graph_sources)
    features_gap_rows = max(0, len(raw_rows) - len(train_frame))

    return {
        **summary,
        "n_trainable_rows": int(len(train_frame)),
        "features_gap_rows": features_gap_rows,
        "rows_by_source_top": dict(sorted(rows_by_source.items(), key=lambda x: -x[1])[:8]),
        "rows_by_peer_top": dict(sorted(rows_by_peer.items(), key=lambda x: -x[1])[:12]),
        "matured_sources_with_graph": len(matured_sources),
        "sources_with_spillover_rows": len(sources_with_rows),
        "sources_without_spillover_rows": sources_without_rows,
        "graph_sources_without_spillover_rows": graph_sources_without_rows,
        "peers_in_graph_not_in_train": peers_in_graph_not_in_train,
        "cold_peers_below_min_samples": cold_peers,
        "min_peer_samples_threshold": max(1, int(min_peer_samples)),
        "universe_symbols_peer_only": universe_sources_never_leader,
    }

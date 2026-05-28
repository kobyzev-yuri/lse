"""API helpers for earnings intelligence UI (list events + Event Brief)."""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.earnings_event_brief import build_event_brief, load_peer_edges, load_peer_spillover_outcomes
from services.earnings_intelligence_universe import get_earnings_intelligence_universe
from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_game_5m


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _group_label(symbol: str) -> str:
    sym = symbol.strip().upper()
    game = {t.strip().upper() for t in get_tickers_game_5m()}
    portfolio = {t.strip().upper() for t in get_tickers_for_portfolio_game()}
    tags: list[str] = []
    if sym in game:
        tags.append("GAME_5M")
    if sym in portfolio:
        tags.append("portfolio")
    if not tags:
        tags.append("context")
    return "+".join(tags)


def list_intelligence_events(
    engine: Engine,
    *,
    since: date | None = None,
    until: date | None = None,
    symbols: list[str] | None = None,
    limit: int = 80,
    past_only: bool = True,
) -> dict[str, Any]:
    """Recent KB earnings rows with materials / LLM / brief readiness flags."""
    universe = symbols or get_earnings_intelligence_universe()
    sym_set = sorted({s.strip().upper() for s in universe if s.strip()})
    if not sym_set:
        return {"events": [], "universe": [], "summary": {}}

    where = [
        "UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'",
        "UPPER(TRIM(kb.ticker)) = ANY(:symbols)",
    ]
    params: dict[str, Any] = {"symbols": sym_set, "limit": max(1, int(limit))}
    if since:
        where.append("kb.ts::date >= :since")
        params["since"] = since
    if until:
        where.append("kb.ts::date <= :until")
        params["until"] = until
    elif past_only:
        where.append("kb.ts::date <= CURRENT_DATE")

    q = text(
        f"""
        SELECT
          kb.id AS knowledge_base_id,
          UPPER(TRIM(kb.ticker)) AS symbol,
          kb.ts::date AS event_date,
          ed.fiscal_period,
          ed.guidance_summary->>'management_tone' AS management_tone,
          ed.guidance_summary->'scenario_hints'->0->>'scenario' AS top_scenario,
          erd.final_label,
          (SELECT count(*) FROM earnings_material em
             WHERE UPPER(em.symbol) = UPPER(TRIM(kb.ticker))
               AND em.event_date = kb.ts::date
               AND em.parse_status IN ('parsed', 'extracted')) AS materials_useful,
          (SELECT count(*) FROM earnings_material em
             WHERE UPPER(em.symbol) = UPPER(TRIM(kb.ticker))
               AND em.event_date = kb.ts::date) AS materials_total
        FROM knowledge_base kb
        LEFT JOIN earnings_event_detail ed ON ed.knowledge_base_id = kb.id
        LEFT JOIN event_reaction_dataset erd
          ON erd.knowledge_base_id = kb.id
         AND erd.dataset_version = 'v0_expanded_baseline'
        WHERE {' AND '.join(where)}
        ORDER BY kb.ts DESC, kb.id DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
        mat_q = text(
            """
            SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
            FROM earnings_material
            WHERE parse_status IN ('parsed', 'extracted')
              AND UPPER(TRIM(symbol)) = ANY(:symbols)
            """
        )
        covered_rows = conn.execute(mat_q, {"symbols": sym_set}).all()

    covered_symbols = {str(r[0]).upper() for r in covered_rows}
    events: list[dict[str, Any]] = []
    llm_count = 0
    materials_count = 0
    for r in rows:
        sym = str(r["symbol"]).upper()
        ev_d = _parse_date(r.get("event_date"))
        has_materials = int(r.get("materials_useful") or 0) > 0
        has_llm = bool(r.get("management_tone"))
        if has_materials:
            materials_count += 1
        if has_llm:
            llm_count += 1
        events.append(
            {
                "knowledge_base_id": r.get("knowledge_base_id"),
                "symbol": sym,
                "event_date": ev_d.isoformat() if ev_d else None,
                "fiscal_period": r.get("fiscal_period"),
                "group": _group_label(sym),
                "materials_useful": int(r.get("materials_useful") or 0),
                "materials_total": int(r.get("materials_total") or 0),
                "has_materials": has_materials,
                "has_llm": has_llm,
                "management_tone": r.get("management_tone"),
                "top_scenario": r.get("top_scenario") or r.get("final_label"),
                "brief_ready": has_llm,
                "brief_url": f"/api/earnings/brief/{sym}"
                + (f"?event_date={ev_d.isoformat()}" if ev_d else ""),
            }
        )

    missing = [s for s in sym_set if s not in covered_symbols]
    return {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "universe": sym_set,
        "events": events,
        "summary": {
            "universe_size": len(sym_set),
            "events_listed": len(events),
            "with_materials": materials_count,
            "with_llm": llm_count,
            "symbols_with_materials": len(covered_symbols),
            "symbols_without_materials": missing,
        },
    }


def get_event_brief_payload(
    engine: Engine,
    *,
    symbol: str,
    event_date: date | None = None,
    dataset_version: str = "v0_expanded_baseline",
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    if event_date is None:
        q = text(
            """
            SELECT kb.ts::date AS event_date
            FROM knowledge_base kb
            WHERE UPPER(TRIM(kb.ticker)) = :symbol
              AND UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
              AND kb.ts::date <= CURRENT_DATE
            ORDER BY kb.ts DESC
            LIMIT 1
            """
        )
        with engine.connect() as conn:
            row = conn.execute(q, {"symbol": sym}).first()
        if not row:
            return {"status": "not_found", "symbol": sym, "reason": "no past KB earnings"}
        event_date = _parse_date(row[0])
        if event_date is None:
            return {"status": "not_found", "symbol": sym, "reason": "bad event date"}

    brief = build_event_brief(
        engine,
        symbol=sym,
        event_date=event_date,
        dataset_version=dataset_version,
    )
    brief["group"] = _group_label(sym)
    return brief


def _log_ret_pct(log_ret: Any) -> str:
    if log_ret is None:
        return "—"
    try:
        x = float(log_ret)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(x):
        return "—"
    pct = (math.expm1(x)) * 100.0
    return f"{pct:+.2f}%"


def format_brief_telegram(brief: dict[str, Any], *, max_peers: int = 6) -> str:
    """Compact Markdown-ish text for Telegram /earnings."""
    if brief.get("status") not in ("ok", "partial"):
        return f"❌ {brief.get('symbol', '?')} {brief.get('event_date', '')}: {brief.get('reason', brief.get('status'))}"

    sym = brief.get("symbol", "")
    ev = brief.get("event_date", "")
    lines = [
        f"📊 *Earnings Brief* — {sym} ({ev})",
        f"_{brief.get('headline') or '—'}_",
        "",
        f"Tone: {brief.get('management_tone') or '—'}",
    ]
    scen = brief.get("scenario") or {}
    if scen.get("id"):
        conf = scen.get("confidence") or ""
        lines.append(f"Scenario: `{scen.get('id')}` ({conf})".rstrip())
    src = brief.get("source_outcomes") or {}
    if src.get("forward_log_ret_1d") is not None:
        lines.append(
            f"Source fwd: 1d {_log_ret_pct(src['forward_log_ret_1d'])} "
            f"5d {_log_ret_pct(src.get('forward_log_ret_5d'))}"
        )
    peers = brief.get("peer_spillover_outcomes") or []
    if peers:
        lines.append("")
        lines.append("*Peer spillover (log-ret)*:")
        for p in peers[:max_peers]:
            if p.get("status") != "ok":
                continue
            t = p.get("ticker")
            r1 = p.get("forward_log_ret_1d")
            r5 = p.get("forward_log_ret_5d")
            if r1 is None:
                continue
            lines.append(f"  {t}: 1d {_log_ret_pct(r1)}" + (f" 5d {_log_ret_pct(r5)}" if r5 is not None else ""))
    quotes = brief.get("evidence_quotes") or []
    if quotes:
        q0 = quotes[0]
        if isinstance(q0, dict):
            txt = str(q0.get("quote") or q0.get("text") or "")[:200]
        else:
            txt = str(q0)[:200]
        if txt:
            lines.extend(["", f"\"{txt}…\"" if len(txt) >= 200 else f"\"{txt}\""])
    lines.append("")
    lines.append(f"Web: /earnings → {sym}")
    return "\n".join(lines)


def get_peer_graph_ui(engine: Engine, *, universe_only: bool = True) -> dict[str, Any]:
    """Peer graph nodes/edges for UI (spillover topology, not daily /corr)."""
    universe = {s.upper() for s in get_earnings_intelligence_universe()}
    q = text(
        """
        SELECT source_ticker, target_ticker, relation_type, weight, meta
        FROM peer_graph_edge
        ORDER BY weight DESC, source_ticker, target_ticker
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()

    edges: list[dict[str, Any]] = []
    nodes: set[str] = set()
    for r in rows:
        src = str(r["source_ticker"]).upper()
        tgt = str(r["target_ticker"]).upper()
        if universe_only and (src not in universe or tgt not in universe):
            continue
        w = float(r["weight"]) if r.get("weight") is not None else 0.0
        edges.append(
            {
                "source": src,
                "target": tgt,
                "relation_type": r.get("relation_type"),
                "weight": round(w, 4),
                "meta": r.get("meta") or {},
            }
        )
        nodes.add(src)
        nodes.add(tgt)

    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}
    for e in edges:
        out_degree[e["source"]] = out_degree.get(e["source"], 0) + 1
        in_degree[e["target"]] = in_degree.get(e["target"], 0) + 1

    node_list = sorted(
        nodes,
        key=lambda n: (-max(out_degree.get(n, 0), in_degree.get(n, 0)), n),
    )
    return {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "nodes": [
            {
                "id": n,
                "group": _group_label(n),
                "out_degree": out_degree.get(n, 0),
                "in_degree": in_degree.get(n, 0),
            }
            for n in node_list
        ],
        "edges": edges,
        "summary": {
            "node_count": len(node_list),
            "edge_count": len(edges),
            "sources_with_edges": len(out_degree),
        },
        "note": (
            "Граф spillover: directed edges source→target с весом влияния. "
            "Это не дневная матрица /corr — здесь якорь = дата earnings source-тикера."
        ),
    }


def get_spillover_history(
    engine: Engine,
    *,
    source_symbol: str,
    since: date | None = None,
    limit: int = 10,
    dataset_version: str = "v0_expanded_baseline",
) -> dict[str, Any]:
    """Historical cross-impact: when source reported, forward returns of graph peers."""
    sym = source_symbol.strip().upper()
    where = [
        "UPPER(TRIM(kb.ticker)) = :symbol",
        "UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'",
        "kb.ts::date <= CURRENT_DATE",
    ]
    params: dict[str, Any] = {"symbol": sym, "limit": max(1, int(limit))}
    if since:
        where.append("kb.ts::date >= :since")
        params["since"] = since
    q = text(
        f"""
        SELECT kb.id AS knowledge_base_id, kb.ts::date AS event_date,
               ed.guidance_summary->>'management_tone' AS management_tone,
               ed.guidance_summary->'scenario_hints'->0->>'scenario' AS top_scenario
        FROM knowledge_base kb
        LEFT JOIN earnings_event_detail ed ON ed.knowledge_base_id = kb.id
        WHERE {' AND '.join(where)}
        ORDER BY kb.ts DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()

    static_peers = load_peer_edges(engine, source_ticker=sym)
    peer_targets = [str(p["target_ticker"]).upper() for p in static_peers]

    events_out: list[dict[str, Any]] = []
    for r in rows:
        ev_d = _parse_date(r.get("event_date"))
        if ev_d is None:
            continue
        brief = build_event_brief(
            engine,
            symbol=sym,
            event_date=ev_d,
            dataset_version=dataset_version,
        )
        src_out = brief.get("source_outcomes") or {}
        peer_rows = brief.get("peer_spillover_outcomes") or []
        events_out.append(
            {
                "event_date": ev_d.isoformat(),
                "management_tone": r.get("management_tone"),
                "top_scenario": r.get("top_scenario"),
                "source_forward_log_ret_1d": src_out.get("forward_log_ret_1d"),
                "source_forward_log_ret_5d": src_out.get("forward_log_ret_5d"),
                "peer_outcomes": peer_rows,
            }
        )

    return {
        "source_symbol": sym,
        "peer_graph_edges": static_peers,
        "peer_targets": peer_targets,
        "events": events_out,
        "note": (
            "Spillover matrix: forward log-returns пиров от даты отчёта source-тикера. "
            "Отличается от /corr (скользящая дневная корреляция котировок)."
        ),
    }


def get_ml_layers_status(
    engine: Engine,
    *,
    dataset_version: str = "v0_expanded_baseline",
) -> dict[str, Any]:
    """Explain regression vs rule labels vs scenario labels vs planned classifier."""
    q = text(
        """
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE outcomes_after ? 'forward_log_ret_5d') AS with_outcomes,
          count(*) FILTER (WHERE final_label IN ('UP','DOWN','FLAT')) AS rule_direction,
          count(*) FILTER (WHERE label_source = 'llm_scenario_v0') AS llm_scenario,
          count(*) FILTER (WHERE final_label NOT IN ('UP','DOWN','FLAT') AND final_label IS NOT NULL) AS named_scenario
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
        """
    )
    with engine.connect() as conn:
        row = conn.execute(q, {"dv": dataset_version}).mappings().first() or {}
        llm_extracted = int(
            conn.execute(
                text(
                    """
                    SELECT count(*) FROM earnings_event_detail ed
                    WHERE ed.guidance_summary->>'management_tone' IS NOT NULL
                    """
                )
            ).scalar()
            or 0
        )
        earnings_v1_rows = int(
            conn.execute(
                text(
                    """
                    SELECT count(*) FROM event_reaction_dataset
                    WHERE dataset_version = :dv
                      AND features_before IS NOT NULL
                      AND features_before <> '{}'::jsonb
                      AND (features_before->>'feature_builder_version') = 'quotes_regime_earnings_v1'
                    """
                ),
                {"dv": dataset_version},
            ).scalar()
            or 0
        )

    total = int(row.get("total") or 0)
    named_scenario = int(row.get("named_scenario") or 0)
    llm_scenario_applied = int(row.get("llm_scenario") or 0)

    scenario_metrics: dict[str, Any] = {}
    scenario_model_path = Path("/app/logs/ml/models/event_reaction_scenario_catboost.cbm")
    metrics_path = Path("/app/logs/ml/ml_data_quality/last_event_reaction_scenario_train_metrics.json")
    if not scenario_model_path.is_file():
        root = Path(__file__).resolve().parents[1]
        scenario_model_path = root / "local/models/event_reaction_scenario_catboost.cbm"
        metrics_path = root / "local/logs/ml_data_quality/last_event_reaction_scenario_train_metrics.json"
    if metrics_path.is_file():
        try:
            import json

            scenario_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except Exception:
            scenario_metrics = {}
    clf_mets = scenario_metrics.get("metrics") if isinstance(scenario_metrics.get("metrics"), dict) else {}
    clf_status = str(scenario_metrics.get("status") or "")
    clf_ready = clf_status == "ok" and scenario_model_path.is_file() and llm_scenario_applied >= 8

    layers = [
        {
            "id": "quotes_regime_earnings_v1",
            "title": "Feature builder quotes_regime_earnings_v1",
            "status": "active" if earnings_v1_rows >= 8 else "pilot",
            "count": earnings_v1_rows,
            "target": "features_before JSON",
            "script": "scripts/run_earnings_ml_refresh.py",
            "description": (
                "Quotes + market_regime + earnings tone/timing + peer_graph (out-degree, weight sum) "
                "+ peer_momentum (mean/max 5d log-ret пиров на as_of). "
                "Backfill: EVENT_REACTION_FEATURE_BUILDER_VERSION=quotes_regime_earnings_v1."
            ),
        },
        {
            "id": "catboost_regression",
            "title": "CatBoost регрессия (prod)",
            "status": "active",
            "target": "outcomes_after.forward_log_ret_5d",
            "api": "/api/ml/event-reaction/{ticker}",
            "description": (
                "Предсказывает log-доходность на 5 торговых дней после earnings. "
                "Признаки: quotes + market_regime (quotes_regime_v1). "
                "Материалы/transcript пока не в feature_builder product-модели."
            ),
        },
        {
            "id": "rule_direction",
            "title": "Правило UP/DOWN/FLAT",
            "status": "active",
            "target": "final_label по порогу forward_log_ret_5d",
            "count": int(row.get("rule_direction") or 0),
            "description": "Backfill из daily quotes — baseline метка направления, не ML-классификатор.",
        },
        {
            "id": "llm_scenario_hints",
            "title": "LLM scenario hints",
            "status": "active" if llm_scenario_applied >= 8 else ("pilot" if llm_extracted else "pending"),
            "count": llm_extracted,
            "applied_labels": llm_scenario_applied,
            "script": "scripts/apply_earnings_scenario_labels.py",
            "description": (
                "Из earnings materials: capex_positive_for_infra_peers, gap_up_follow_through и др. "
                "Пишется в earnings_event_detail, опционально в final_label."
            ),
        },
        {
            "id": "scenario_classifier",
            "title": "CatBoost классификатор сценариев",
            "status": "active" if clf_ready else ("pilot" if named_scenario >= 8 else "planned"),
            "script": "scripts/train_event_reaction_scenario_classifier.py",
            "model_path": str(scenario_model_path) if scenario_model_path.is_file() else None,
            "metrics": {
                "valid_accuracy": clf_mets.get("valid_accuracy"),
                "n_train": clf_mets.get("n_train"),
                "n_valid": clf_mets.get("n_valid"),
                "classes": clf_mets.get("classes"),
            },
            "blocked_by": (
                []
                if clf_ready
                else (
                    []
                    if named_scenario >= 8
                    else [f"≥8 LLM scenario labels (сейчас ~{named_scenario})", "backfill quotes_regime_earnings_v1"]
                )
            ),
            "description": (
                "Multi-class по final_label (llm_scenario_v0): gap_up_follow_through, "
                "capex_positive_for_infra_peers и др. Запуск: run_earnings_ml_refresh.py."
            ),
        },
    ]

    return {
        "dataset_version": dataset_version,
        "dataset_rows": total,
        "rows_with_outcomes": int(row.get("with_outcomes") or 0),
        "llm_scenario_labels_applied": llm_scenario_applied,
        "earnings_v1_feature_rows": earnings_v1_rows,
        "scenario_classifier_ready": clf_ready,
        "named_scenario_labels": named_scenario,
        "layers": layers,
        "daily_corr_note": (
            "Дневная корреляция (/corr, portfolio cards) — фоновая связь котировок. "
            "Peer graph + spillover — event-study от даты отчёта лидера."
        ),
    }

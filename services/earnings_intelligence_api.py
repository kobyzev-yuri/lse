"""API helpers for earnings intelligence UI (list events + Event Brief)."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.earnings_event_brief import build_event_brief
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

    events: list[dict[str, Any]] = []
    covered_symbols: set[str] = set()
    llm_count = 0
    materials_count = 0
    for r in rows:
        sym = str(r["symbol"]).upper()
        ev_d = _parse_date(r.get("event_date"))
        has_materials = int(r.get("materials_useful") or 0) > 0
        has_llm = bool(r.get("management_tone"))
        if has_materials:
            materials_count += 1
            covered_symbols.add(sym)
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

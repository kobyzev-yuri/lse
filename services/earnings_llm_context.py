"""Earnings context for entry LLM: last own report + gated peer spillover by game horizon."""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _normalize_strategy(strategy_name: str | None) -> str:
    s = (strategy_name or "").strip().upper().replace("-", "_")
    if s in ("GAME_5M", "5M", "GAME5M"):
        return "GAME_5M"
    return "PORTFOLIO"


def spillover_relevance_days(strategy_name: str | None) -> int:
    """Calendar days after leader report when spillover is still relevant for the game."""
    strat = _normalize_strategy(strategy_name)
    if strat == "GAME_5M":
        raw = (get_config_value("EARNINGS_LLM_SPILLOVER_DAYS_GAME5M", "2") or "2").strip()
        default = 2
    else:
        raw = (get_config_value("EARNINGS_LLM_SPILLOVER_DAYS_PORTFOLIO", "5") or "5").strip()
        default = 5
    try:
        return max(0, min(20, int(float(raw))))
    except (TypeError, ValueError):
        return default


def _today_et() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return date.today()


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _log_ret_pct(log_ret: Any) -> str:
    if log_ret is None:
        return "—"
    try:
        x = float(log_ret)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(x):
        return "—"
    return f"{math.expm1(x) * 100.0:+.2f}%"


def _load_peer_spillover_train_metrics() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    for path in (
        Path("/app/logs/ml/ml_data_quality/last_peer_spillover_train_metrics.json"),
        root / "local/logs/ml_data_quality/last_peer_spillover_train_metrics.json",
    ):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            mets = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
            return {
                "status": raw.get("status"),
                "n_train": mets.get("n_train"),
                "rmse_valid": mets.get("rmse_valid"),
                "sign_accuracy_valid": mets.get("sign_accuracy_valid"),
                "baseline_sign_accuracy_valid": mets.get("baseline_sign_accuracy_valid"),
            }
        except Exception as e:
            logger.debug("peer spillover metrics %s: %s", path, e)
    return {}


def _find_latest_own_earnings(engine: Any, symbol: str, as_of: date) -> dict[str, Any] | None:
    q = text(
        """
        SELECT kb.id AS knowledge_base_id, kb.ts::date AS event_date
        FROM knowledge_base kb
        WHERE UPPER(TRIM(kb.ticker)) = :symbol
          AND UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
          AND kb.ts::date <= :as_of
        ORDER BY kb.ts::date DESC, kb.id DESC
        LIMIT 1
        """
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(q, {"symbol": symbol, "as_of": as_of}).mappings().first()
        if not row:
            return None
        ev_d = _parse_date(row.get("event_date"))
        if ev_d is None:
            return None
        return {"event_date": ev_d, "knowledge_base_id": row.get("knowledge_base_id")}
    except Exception as e:
        logger.debug("earnings_llm latest own %s: %s", symbol, e)
        return None


def _find_recent_spillover_source(
    engine: Any,
    symbol: str,
    as_of: date,
    horizon_days: int,
) -> dict[str, Any] | None:
    if horizon_days <= 0:
        return None
    q = text(
        """
        SELECT
          UPPER(TRIM(kb.ticker)) AS source_symbol,
          kb.ts::date AS event_date,
          kb.id AS knowledge_base_id,
          pge.relation_type,
          pge.weight
        FROM knowledge_base kb
        JOIN peer_graph_edge pge ON UPPER(pge.source_ticker) = UPPER(TRIM(kb.ticker))
        WHERE UPPER(pge.target_ticker) = :symbol
          AND UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
          AND kb.ts::date <= :as_of
          AND kb.ts::date >= :lo
        ORDER BY kb.ts::date DESC, pge.weight DESC NULLS LAST, kb.id DESC
        LIMIT 1
        """
    )
    params = {
        "symbol": symbol,
        "as_of": as_of,
        "lo": as_of - timedelta(days=horizon_days),
    }
    try:
        with engine.connect() as conn:
            row = conn.execute(q, params).mappings().first()
        if not row:
            return None
        ev_d = _parse_date(row.get("event_date"))
        if ev_d is None:
            return None
        return {
            "source_symbol": str(row.get("source_symbol") or "").upper(),
            "event_date": ev_d,
            "knowledge_base_id": row.get("knowledge_base_id"),
            "relation_type": row.get("relation_type"),
            "weight": row.get("weight"),
        }
    except Exception as e:
        logger.debug("earnings_llm spillover %s: %s", symbol, e)
        return None


def _brief_slice(
    engine: Any,
    *,
    source_sym: str,
    event_date: date,
    target_sym: str,
    strategy: str,
    spillover_horizon: int,
) -> dict[str, Any]:
    from services.earnings_event_brief import build_event_brief

    brief = build_event_brief(engine, symbol=source_sym, event_date=event_date)
    scen = brief.get("scenario") if isinstance(brief.get("scenario"), dict) else {}
    scen_ml = brief.get("scenario_ml") if isinstance(brief.get("scenario_ml"), dict) else {}
    ref = _today_et()
    days_from = (ref - event_date).days

    out: dict[str, Any] = {
        "source_symbol": source_sym,
        "event_date": event_date.isoformat(),
        "days_from_report": days_from,
        "brief_status": brief.get("status"),
        "headline": brief.get("headline"),
        "management_tone": brief.get("management_tone"),
        "scenario_id": scen.get("id"),
        "scenario_confidence": scen.get("confidence"),
        "scenario_rationale": scen.get("rationale"),
        "scenario_ml": {
            "predicted_scenario": scen_ml.get("predicted_scenario"),
            "predicted_scenario_proba": scen_ml.get("predicted_scenario_proba"),
            "predicted_scenario_sign": scen_ml.get("predicted_scenario_sign"),
        },
        "evidence_quotes": (brief.get("evidence_quotes") or [])[:2],
    }

    if source_sym == target_sym:
        src_out = brief.get("source_outcomes") if isinstance(brief.get("source_outcomes"), dict) else {}
        out["source_outcomes_1d_pct"] = _log_ret_pct(src_out.get("forward_log_ret_1d"))
        out["source_outcomes_5d_pct"] = _log_ret_pct(src_out.get("forward_log_ret_5d"))
        try:
            from services.event_reaction_catboost_signal import predict_event_reaction_for_ticker

            er = predict_event_reaction_for_ticker(target_sym, event_date=event_date)
            out["regression_ml"] = {
                "status": er.get("event_reaction_ml_status"),
                "direction": er.get("event_reaction_ml_direction"),
                "expected_return_5d_pct": er.get("event_reaction_ml_expected_return_5d_pct"),
                "forward_log_ret_5d_pred": er.get("event_reaction_ml_forward_log_ret_5d_pred"),
                "entry_score": er.get("event_reaction_ml_entry_score"),
                "rmse_valid": er.get("event_reaction_ml_rmse_valid"),
            }
        except Exception as e:
            logger.debug("earnings_llm regression %s: %s", target_sym, e)
    else:
        peer_fact = None
        peer_ml_row = None
        for p in brief.get("peer_spillover_outcomes") or []:
            if str(p.get("ticker") or "").upper() == target_sym and p.get("status") == "ok":
                peer_fact = p
                break
        for p in brief.get("peer_spillover_ml") or []:
            if str(p.get("peer_ticker") or "").upper() == target_sym:
                peer_ml_row = p
                break
        out["peer_relation"] = None
        out["peer_edge_weight"] = None
        if peer_fact:
            out["peer_spillover_fact"] = {
                "forward_log_ret_1d_pct": _log_ret_pct(peer_fact.get("forward_log_ret_1d")),
                "forward_log_ret_5d_pct": _log_ret_pct(peer_fact.get("forward_log_ret_5d")),
            }
        if peer_ml_row and strategy != "GAME_5M":
            out["peer_spillover_ml"] = {
                "status": peer_ml_row.get("peer_spillover_ml_status"),
                "pred_5d_pct": _log_ret_pct(peer_ml_row.get("peer_forward_log_ret_5d_pred")),
            }
        out["spillover_horizon_days"] = spillover_horizon
        out["spillover_game_horizon_note"] = (
            "GAME_5M: учитывай только 1d spillover fact; ML 5d не показан — горизонт игры короче."
            if strategy == "GAME_5M"
            else f"PORTFOLIO: spillover релевантен в первые {spillover_horizon} календ.дн. после отчёта лидера."
        )

    return out


def build_earnings_entry_context(
    symbol: str,
    *,
    strategy_name: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """
    Own report: последний прошлый earnings тикера (без окна — длительность эффекта неизвестна).
    Spillover: только если отчёт лидера в пределах горизонта игры (2д game5m / 5д portfolio).
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    strategy = _normalize_strategy(strategy_name)
    spill_horizon = spillover_relevance_days(strategy)
    ref = as_of or _today_et()

    try:
        from report_generator import get_engine

        engine = get_engine()
    except Exception as e:
        logger.debug("earnings_llm context engine: %s", e)
        return None

    own_row = _find_latest_own_earnings(engine, sym, ref)
    spill_row = _find_recent_spillover_source(engine, sym, ref, spill_horizon)

    own_report: dict[str, Any] | None = None
    spillover: dict[str, Any] | None = None

    if own_row:
        own_report = _brief_slice(
            engine,
            source_sym=sym,
            event_date=own_row["event_date"],
            target_sym=sym,
            strategy=strategy,
            spillover_horizon=spill_horizon,
        )
        own_report["role"] = "own"

    if spill_row:
        src = spill_row["source_symbol"]
        if not own_report or own_row["event_date"] != spill_row["event_date"] or src != sym:
            spillover = _brief_slice(
                engine,
                source_sym=src,
                event_date=spill_row["event_date"],
                target_sym=sym,
                strategy=strategy,
                spillover_horizon=spill_horizon,
            )
            spillover["role"] = "peer_spillover"
            spillover["peer_relation"] = spill_row.get("relation_type")
            spillover["peer_edge_weight"] = spill_row.get("weight")

    if not own_report and not spillover:
        return None

    return {
        "symbol": sym,
        "strategy": strategy,
        "spillover_horizon_days": spill_horizon,
        "peer_spillover_model_metrics": _load_peer_spillover_train_metrics(),
        "own_report": own_report,
        "spillover": spillover,
    }


def _format_days_from(days: Any) -> str:
    if days is None:
        return "—"
    try:
        d = int(days)
    except (TypeError, ValueError):
        return "—"
    if d == 0:
        return "в день отчёта"
    if d > 0:
        return f"+{d} календ.дн. после отчёта"
    return f"{d} календ.дн. до отчёта"


def _format_report_block(section: dict[str, Any], *, target_sym: str, is_own: bool) -> list[str]:
    lines: list[str] = []
    src = section.get("source_symbol", target_sym)
    ev = section.get("event_date", "?")
    when = _format_days_from(section.get("days_from_report"))

    if is_own:
        lines.append(f"### Последний отчёт {target_sym} ({when}, {ev})")
        lines.append(
            "Длительность влияния не фиксирована (зависит от отчёта и масштаба компании) — "
            "используй как фон, не как таймер."
        )
    else:
        rel = section.get("peer_relation") or "peer"
        w = section.get("peer_edge_weight")
        wtxt = f", graph weight {float(w):.2f}" if w is not None else ""
        lines.append(f"### Spillover от {src} → {target_sym} ({when}, {ev}, {rel}{wtxt})")
        note = section.get("spillover_game_horizon_note")
        if note:
            lines.append(str(note))

    if section.get("headline"):
        lines.append(f"- Headline: {section['headline']}")
    if section.get("management_tone"):
        lines.append(f"- Tone: {section['management_tone']}")
    if section.get("scenario_id"):
        conf = section.get("scenario_confidence") or ""
        lines.append(f"- LLM scenario: {section['scenario_id']}" + (f" ({conf})" if conf else ""))
    if section.get("scenario_rationale"):
        lines.append(f"- Rationale: {str(section['scenario_rationale'])[:220]}")

    sm = section.get("scenario_ml") if isinstance(section.get("scenario_ml"), dict) else {}
    if sm.get("predicted_scenario"):
        proba = sm.get("predicted_scenario_proba")
        ptxt = f", proba {float(proba)*100:.0f}%" if proba is not None else ""
        lines.append(f"- ML scenario (advisory): {sm['predicted_scenario']}{ptxt}")

    if is_own:
        reg = section.get("regression_ml") if isinstance(section.get("regression_ml"), dict) else {}
        if reg.get("status") == "ok":
            rmse = reg.get("rmse_valid")
            rmse_txt = f", RMSE_valid={float(rmse):.4f}" if rmse is not None else ""
            lines.append(
                f"- ML regression 5d: {reg.get('direction', '—')} "
                f"≈{reg.get('expected_return_5d_pct', '—')}% "
                f"(score {reg.get('entry_score', '—')}{rmse_txt})"
            )
        o1 = section.get("source_outcomes_1d_pct")
        o5 = section.get("source_outcomes_5d_pct")
        if o1 and o1 != "—":
            lines.append(f"- Факт после отчёта: 1d {o1}" + (f", 5d {o5}" if o5 and o5 != "—" else ""))
    else:
        pf = section.get("peer_spillover_fact") if isinstance(section.get("peer_spillover_fact"), dict) else {}
        if pf.get("forward_log_ret_1d_pct"):
            lines.append(f"- Spillover fact 1d: {pf.get('forward_log_ret_1d_pct', '—')}")
        if pf.get("forward_log_ret_5d_pct") and pf.get("forward_log_ret_5d_pct") != "—":
            lines.append(f"- Spillover fact 5d: {pf.get('forward_log_ret_5d_pct', '—')}")
        pm = section.get("peer_spillover_ml") if isinstance(section.get("peer_spillover_ml"), dict) else {}
        if pm.get("status") == "ok":
            lines.append(f"- Spillover ML pred 5d: {pm.get('pred_5d_pct', '—')}")

    for q in section.get("evidence_quotes") or []:
        if not isinstance(q, dict):
            continue
        txt = str(q.get("quote") or "").strip()
        if txt:
            topic = str(q.get("topic") or "").strip()
            prefix = f"[{topic}] " if topic and topic != "other" else ""
            lines.append(f'- Цитата: "{prefix}{txt[:160]}"')

    return lines


def format_earnings_entry_context_for_llm(ctx: dict[str, Any]) -> str:
    if not ctx:
        return ""
    lines: list[str] = []
    strat = ctx.get("strategy", "PORTFOLIO")
    lines.append(
        f"Контекст earnings для {ctx.get('symbol', '?')} · игра {strat} · "
        "advisory, не заменяет техсигнал."
    )

    own = ctx.get("own_report")
    if isinstance(own, dict):
        lines.extend(_format_report_block(own, target_sym=str(ctx.get("symbol", "")), is_own=True))

    spill = ctx.get("spillover")
    if isinstance(spill, dict):
        if own:
            lines.append("")
        lines.extend(_format_report_block(spill, target_sym=str(ctx.get("symbol", "")), is_own=False))
        mets = ctx.get("peer_spillover_model_metrics") if isinstance(ctx.get("peer_spillover_model_metrics"), dict) else {}
        if mets:
            parts = []
            if mets.get("status"):
                parts.append(f"status={mets['status']}")
            if mets.get("n_train") is not None:
                parts.append(f"n_train={mets['n_train']}")
            if mets.get("sign_accuracy_valid") is not None:
                parts.append(f"sign_acc_valid={float(mets['sign_accuracy_valid'])*100:.0f}%")
            if mets.get("baseline_sign_accuracy_valid") is not None:
                parts.append(f"baseline_sign={float(mets['baseline_sign_accuracy_valid'])*100:.0f}%")
            if mets.get("rmse_valid") is not None:
                parts.append(f"RMSE_valid={float(mets['rmse_valid']):.4f}")
            if parts:
                lines.append(
                    "- Качество spillover ML (pilot): " + ", ".join(parts)
                    + " — низкая уверенность, не опирайся на pred как на KPI."
                )

    return "\n".join(lines)


def attach_earnings_entry_context(
    ticker: str,
    technical_data: dict[str, Any] | None,
    *,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    td = dict(technical_data or {})
    if td.get("earnings_entry_context_block"):
        return td
    strat = strategy_name or td.get("entry_strategy_name")
    ctx = build_earnings_entry_context(ticker, strategy_name=strat)
    if not ctx:
        return td
    td["earnings_entry_context"] = ctx
    td["earnings_entry_context_block"] = format_earnings_entry_context_for_llm(ctx)
    return td

# -*- coding: utf-8 -*-
"""Мониторинг влияния earnings_trust на decision_stack (watchlist + история сделок)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from services.decision_stack._types import gate_mode
from services.decision_stack.game5m import summarize_earnings_trust_impact
from services.earnings_event_postmortem import load_postmortem_rows
from services.earnings_trust_runtime import (
    build_earnings_trust_runtime,
    earnings_trust_gate_mode,
)


def _cfg_int(key: str, default: int) -> int:
    from config_loader import get_config_value

    try:
        return int((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _cfg_bool(key: str, default: bool = False) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _event_date(row: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(row.get("event_date") or "")[:10])
    except ValueError:
        return None


def collect_earnings_trust_watchlist_tickers(*, project_root=None) -> List[str]:
    """Тикеры с активным post-mortem в окне EARNINGS_TRUST_RUNTIME_MAX_AGE_DAYS."""
    window = _cfg_int("EARNINGS_TRUST_RUNTIME_MAX_AGE_DAYS", 21)
    cutoff = date.today() - timedelta(days=max(1, window))
    tickers: set[str] = set()
    for row in load_postmortem_rows(project_root):
        ev_d = _event_date(row)
        if ev_d is None or ev_d < cutoff:
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            tickers.add(sym)
        models = row.get("models") if isinstance(row.get("models"), dict) else {}
        for peer in models.get("peer_spillover") or []:
            if not isinstance(peer, dict):
                continue
            p = str(peer.get("peer") or "").strip().upper()
            if p:
                tickers.add(p)
    return sorted(tickers)


def runtime_gate_summary_for_ticker(ticker: str, *, project_root=None) -> Dict[str, Any]:
    """Сводка gate для одного тикера (UI post-mortem, API)."""
    sym = str(ticker or "").strip().upper()
    if not sym:
        return {"active": False, "reason": "empty_ticker"}

    gm = earnings_trust_gate_mode()
    resolve_on = _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False)
    runtime = build_earnings_trust_runtime(sym, project_root=project_root)
    if not runtime.get("active"):
        return {
            "active": False,
            "ticker": sym,
            "gate_mode": gm,
            "resolve_enabled": resolve_on,
            "reason": runtime.get("reason") or "no_recent_postmortem",
        }

    strength = float(runtime.get("strength") or 0.0)
    would_down = bool(runtime.get("would_downgrade"))
    action = "telemetry"
    if gm == "apply" and would_down:
        action = "downgrade"
    elif gm == "apply" and strength > 0.2:
        action = "boost"

    return {
        "active": True,
        "ticker": sym,
        "gate_mode": gm,
        "resolve_enabled": resolve_on,
        "action": action,
        "strength": runtime.get("strength"),
        "would_downgrade": would_down,
        "detail_ru": runtime.get("detail_ru"),
        "runtime_role": runtime.get("runtime_role"),
        "source_symbol": runtime.get("source_symbol"),
        "event_date": runtime.get("event_date"),
        "trust_labels": runtime.get("trust_labels"),
        "shadow_if_core_bull": "HOLD" if action == "downgrade" and gm == "apply" else "unchanged",
        "live_if_core_bull": (
            "HOLD"
            if action == "downgrade" and gm == "apply" and resolve_on
            else ("shadow_only" if action == "downgrade" and gm == "apply" else "unchanged")
        ),
        "monitor_note_ru": (
            "При core=BUY/STRONG_BUY и apply gate projected resolve → HOLD (shadow)."
            if action == "downgrade" and gm == "apply" and not resolve_on
            else (
                "При core=BUY/STRONG_BUY gate понизит effective до HOLD (live resolve)."
                if action == "downgrade" and gm == "apply" and resolve_on
                else "Только telemetry — на сделки не влияет."
            )
        ),
    }


def _impact_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    cached = snap.get("earnings_trust_impact")
    if isinstance(cached, dict) and cached.get("active"):
        return cached
    contributions = snap.get("contributions") if isinstance(snap.get("contributions"), list) else []
    core = str(snap.get("core_decision") or "HOLD")
    legacy = str((snap.get("legacy") or {}).get("technical_decision_effective") or snap.get("effective_decision") or core)
    projected = str(snap.get("projected_effective_if_resolve") or legacy)
    return summarize_earnings_trust_impact(contributions, core=core, legacy_eff=legacy, projected=projected)


def build_earnings_trust_gate_monitor(
    closed_trades: List[Any],
    *,
    limit: int = 30,
    project_root=None,
) -> Dict[str, Any]:
    """
    Watchlist по post-mortem + последние сделки, где earnings_trust влиял на projected resolve.
    """
    from services.deal_params_5m import normalize_entry_context

    gm = earnings_trust_gate_mode()
    resolve_on = _cfg_bool("DECISION_STACK_RESOLVE_ENABLED", False)
    watchlist: List[Dict[str, Any]] = []
    for sym in collect_earnings_trust_watchlist_tickers(project_root=project_root):
        summary = runtime_gate_summary_for_ticker(sym, project_root=project_root)
        if summary.get("active"):
            watchlist.append(summary)

    would_down_tickers = [
        w["ticker"]
        for w in watchlist
        if w.get("action") == "downgrade" and gm == "apply"
    ]

    trade_rows: List[Dict[str, Any]] = []
    with_et = 0
    et_changed_projected = 0
    for t in closed_trades:
        es = (getattr(t, "entry_strategy", None) or "").strip().upper()
        if es != "GAME_5M":
            continue
        ctx = normalize_entry_context(getattr(t, "context_json", None))
        snap = ctx.get("decision_snapshot")
        if not isinstance(snap, dict):
            continue
        impact = _impact_from_snapshot(snap)
        if not impact.get("active"):
            continue
        with_et += 1
        if impact.get("changed_projected_resolve"):
            et_changed_projected += 1
        trade_rows.append(
            {
                "trade_id": getattr(t, "trade_id", None),
                "ticker": getattr(t, "ticker", None),
                "entry_ts": getattr(t, "entry_ts", None),
                "gate_mode": impact.get("gate_mode"),
                "action": impact.get("action"),
                "strength": impact.get("strength"),
                "detail": impact.get("detail"),
                "legacy_effective": impact.get("legacy_effective"),
                "projected_effective": impact.get("projected_effective"),
                "changed_projected_resolve": impact.get("changed_projected_resolve"),
                "event_date": impact.get("event_date"),
                "runtime_role": impact.get("runtime_role"),
            }
        )

    trade_rows = sorted(
        trade_rows,
        key=lambda r: (str(r.get("entry_ts") or ""), int(r.get("trade_id") or 0)),
        reverse=True,
    )[: max(1, int(limit))]

    return {
        "mode": "earnings_trust_gate_monitor",
        "description": (
            "Активные post-mortem тикеры и влияние earnings_trust на projected resolve. "
            "Live-исполнение — только при DECISION_STACK_RESOLVE_ENABLED=true."
        ),
        "gate_mode": gm,
        "resolve_enabled": resolve_on,
        "max_age_days": _cfg_int("EARNINGS_TRUST_RUNTIME_MAX_AGE_DAYS", 21),
        "watchlist_count": len(watchlist),
        "watchlist_would_downgrade": would_down_tickers,
        "watchlist": watchlist,
        "trades_with_earnings_trust": with_et,
        "trades_earnings_trust_changed_projected": et_changed_projected,
        "recent_trade_impacts": trade_rows,
        "ops_hint_ru": (
            "Включите DECISION_STACK_EARNINGS_TRUST_GATE_MODE=apply для shadow downgrade; "
            "DECISION_STACK_RESOLVE_ENABLED=true — для live."
            if gm == "log_only"
            else (
                "Gate apply активен (shadow). RESOLVE=false — сделки не меняются."
                if not resolve_on
                else "Gate apply + RESOLVE — earnings_trust участвует в effective."
            )
        ),
    }

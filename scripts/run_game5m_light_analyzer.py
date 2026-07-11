#!/usr/bin/env python3
"""
Облегчённый анализатор GAME_5M без загрузки 5m OHLC (избегает OOM в lse-bot).

Секции: core summary, decision_stack shadow, catboost fusion, ml_production_arbiter,
post-mortem rolling, market_adapt guard counterfactual.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.analyzer_ml_arbiter import build_ml_production_arbiter  # noqa: E402
from services.deal_params_5m import normalize_entry_context  # noqa: E402
from services.decision_stack.earnings_trust_monitor import build_earnings_trust_gate_monitor  # noqa: E402
from services.trade_effectiveness_analyzer import (  # noqa: E402
    _aggregate,
    _build_catboost_entry_backtest,
    _build_decision_stack_shadow_diff,
    _build_game5m_catboost_fusion_entry_review,
    _build_game5m_catboost_status,
    _estimate_trade_effects,
    _load_closed_trades,
    _load_weekly_game5m_tactic_review_artifact,
)


def _default_out_path() -> Path:
    if Path("/app/logs/ml/ml_data_quality").exists():
        return Path("/app/logs/ml/ml_data_quality/analyzer_7d_light.json")
    return project_root / "local/logs/ml/ml_data_quality/analyzer_7d_light.json"


def _load_postmortem_sessions(path: Path, *, max_sessions: int = 7) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-max_sessions:]


def _build_market_adapt_guard_review(closed: List[Any]) -> Dict[str, Any]:
    """Контрфакт: gap≤−2% или entry_advice CAUTION/AVOID → legacy BUY без guard."""
    rows: List[Dict[str, Any]] = []
    n_gap_down_buy = 0
    n_would_block_gap = 0
    n_would_block_advice = 0
    sum_pnl_blocked = 0.0

    for t in closed:
        ctx = normalize_entry_context(getattr(t, "context_json", None))
        core = ctx.get("technical_decision_core") or ctx.get("decision")
        eff = ctx.get("technical_decision_effective") or core
        if str(core or "").upper() not in ("BUY", "STRONG_BUY"):
            continue
        gap = ctx.get("premarket_gap_pct")
        try:
            gap_f = float(gap) if gap is not None else None
        except (TypeError, ValueError):
            gap_f = None
        advice = (ctx.get("entry_advice") or "ALLOW").strip().upper()
        snap = ctx.get("decision_snapshot") if isinstance(ctx.get("decision_snapshot"), dict) else {}
        projected = snap.get("projected_effective_if_resolve")
        cost = float(getattr(t, "net_pnl", 0) or 0)
        qty = float(getattr(t, "quantity", 0) or 0)
        entry = float(getattr(t, "entry_price", 0) or 0)
        pnl_pct = (cost / (qty * entry) * 100) if qty > 0 and entry > 0 else None

        would_gap = gap_f is not None and gap_f <= -2.0
        would_advice = advice in ("CAUTION", "AVOID")
        would_block = would_gap or would_advice
        if would_gap:
            n_gap_down_buy += 1
            if would_block:
                n_would_block_gap += 1
        if would_advice:
            n_would_block_advice += 1
        if would_block:
            sum_pnl_blocked += cost

        rows.append(
            {
                "trade_id": getattr(t, "trade_id", None),
                "ticker": getattr(t, "ticker", None),
                "premarket_gap_pct": gap_f,
                "entry_advice": advice,
                "technical_decision_core": core,
                "technical_decision_effective": eff,
                "stack_projected_if_resolve": projected,
                "would_block_market_adapt_v1": would_block,
                "realized_net_pnl": round(cost, 2),
                "realized_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
                "entry_guard_applied": bool(ctx.get("game5m_entry_guard_applied")),
            }
        )

    return {
        "mode": "market_adapt_guard_counterfactual",
        "description": "Сделки с core BUY/STRONG_BUY: блок при gap≤−2% или CAUTION/AVOID (market_adapt_v1).",
        "n_core_bullish": len(rows),
        "n_gap_down_le_2pct": n_gap_down_buy,
        "n_would_block_gap": n_would_block_gap,
        "n_would_block_advice": n_would_block_advice,
        "sum_net_pnl_would_block": round(sum_pnl_blocked, 2),
        "per_trade": rows,
    }


def _json_default(o: Any) -> str:
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    raise TypeError(type(o))


def run_light_analyzer(*, days: int = 7, strategy: str = "GAME_5M") -> Dict[str, Any]:
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    effects = _estimate_trade_effects(closed, {})
    summary = _aggregate(effects)

    payload: Dict[str, Any] = {
        "meta": {
            "days": days,
            "strategy": strategy,
            "trades_analyzed": len(closed),
            "light_mode": True,
            "light_variant": "no_ohlc",
            "sections_applied": [
                "core",
                "decision_stack_shadow_diff",
                "catboost",
                "ml_arbiters",
                "market_adapt_guard",
                "postmortem",
            ],
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "analyzer_source": "scripts/run_game5m_light_analyzer.py",
        },
        "summary": summary,
        "decision_stack_shadow_diff": _build_decision_stack_shadow_diff(strategy, closed, effects, limit=30),
        "game5m_catboost_fusion_entry_review": _build_game5m_catboost_fusion_entry_review(
            strategy, closed, effects
        ),
        "catboost_entry_backtest": _build_catboost_entry_backtest(strategy, closed, effects),
        "game5m_catboost_status": _build_game5m_catboost_status(),
        "earnings_trust_gate_monitor": build_earnings_trust_gate_monitor(closed, limit=20),
        "weekly_game5m_tactic_review": _load_weekly_game5m_tactic_review_artifact(days=days),
        "market_adapt_guard_review": _build_market_adapt_guard_review(closed),
    }
    payload["ml_production_arbiter"] = build_ml_production_arbiter(payload, strategy=strategy)

    pm_path = Path("/app/logs/ml/ml_data_quality/game5m_trade_postmortem_sessions.jsonl")
    if not pm_path.is_file():
        pm_path = project_root / "local/logs/ml/ml_data_quality/game5m_trade_postmortem_sessions.jsonl"
    sessions = _load_postmortem_sessions(pm_path, max_sessions=days)
    if sessions:
        payload["postmortem_sessions_rolling"] = {
            "source": str(pm_path),
            "sessions": sessions,
            "tag_totals": _postmortem_tag_totals(sessions),
        }
    return payload


def _postmortem_tag_totals(sessions: List[Dict[str, Any]]) -> Dict[str, int]:
    totals: Dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
    for s in sessions:
        tags = s.get("tag_counts") if isinstance(s.get("tag_counts"), dict) else {}
        for k in totals:
            try:
                totals[k] += int(tags.get(k) or 0)
            except (TypeError, ValueError):
                pass
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="GAME_5M light analyzer (no OHLC)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--strategy", default="GAME_5M")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    out_path = Path(args.json_out) if args.json_out else _default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = run_light_analyzer(days=max(1, min(30, args.days)), strategy=args.strategy)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "path": str(out_path), "meta": payload.get("meta")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

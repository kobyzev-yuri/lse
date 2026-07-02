# -*- coding: utf-8 -*-
"""Weekly GAME_5M tactic scorecard: bundles, counterfactuals, experiment observe."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from services.deal_params_5m import normalize_entry_context
from services.game5m_active_tactic import TACTIC_CONTEXT_KEYS, get_active_tactic_snapshot
from services.game5m_tuning_ledger import closed_summary, load_ledger


def default_weekly_review_path(project_root=None):
    from pathlib import Path

    root = project_root or Path(__file__).resolve().parents[1]
    return Path("/app/logs/ml/ml_data_quality/last_weekly_game5m_tactic_review.json")


def _aggregate_trades_by_bundle(closed_trades: Sequence[Any], effects: Sequence[Any]) -> List[Dict[str, Any]]:
    by_tid = {int(getattr(t, "trade_id", 0) or 0): t for t in closed_trades}
    buckets: Dict[str, List[float]] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for e in effects:
        tp = by_tid.get(int(e.trade_id))
        if not tp:
            continue
        ctx = normalize_entry_context(getattr(tp, "context_json", None))
        bid = str(ctx.get("active_bundle_id") or "unknown")
        buckets.setdefault(bid, []).append(float(e.realized_pct))
        if bid not in meta:
            meta[bid] = {
                "active_bundle_id": bid if bid != "unknown" else None,
                "active_experiment_id": ctx.get("active_experiment_id"),
            }
    rows: List[Dict[str, Any]] = []
    for bid, pnls in sorted(buckets.items(), key=lambda x: -len(x[1])):
        rows.append(
            {
                **meta.get(bid, {}),
                "bundle_key": bid,
                "n": len(pnls),
                "avg_realized_pct": round(sum(pnls) / len(pnls), 4) if pnls else None,
                "wins": sum(1 for p in pnls if p > 0),
            }
        )
    return rows


def _experiment_review(active: Optional[Dict[str, Any]], *, observe_days: int) -> Dict[str, Any]:
    if not active:
        return {"active": False}
    baseline = active.get("baseline_summary") if isinstance(active.get("baseline_summary"), dict) else {}
    observations = active.get("observations") if isinstance(active.get("observations"), list) else []
    latest_obs = observations[-1] if observations else None
    latest_summary = (latest_obs or {}).get("summary") if isinstance(latest_obs, dict) else None
    current = closed_summary(observe_days)

    def _delta(key: str) -> Optional[float]:
        if not isinstance(latest_summary, dict) or not isinstance(baseline, dict):
            return None
        try:
            a = float(latest_summary.get(key)) if latest_summary.get(key) is not None else None
            b = float(baseline.get(key)) if baseline.get(key) is not None else None
            if a is None or b is None:
                return None
            return round(a - b, 4)
        except (TypeError, ValueError):
            return None

    return {
        "active": True,
        "experiment_id": active.get("experiment_id"),
        "bundle_id": active.get("bundle_id"),
        "kind": active.get("kind"),
        "status": active.get("status"),
        "observe_days": active.get("observe_days"),
        "baseline_summary": baseline,
        "latest_observation": latest_obs,
        "current_window_summary": current,
        "delta_avg_log_return": _delta("avg_log_return"),
        "delta_win_rate_pct": _delta("win_rate_pct"),
        "delta_closed_trades": (
            int(current.get("closed_trades") or 0) - int(baseline.get("closed_trades") or 0)
            if baseline
            else None
        ),
    }


def _recommendations_ru(
    *,
    hold_to_gap: Dict[str, Any],
    experiment: Dict[str, Any],
    active_tactic: Dict[str, Any],
    by_bundle: List[Dict[str, Any]],
) -> List[str]:
    recs: List[str] = []
    cfg = hold_to_gap.get("config_snapshot") if isinstance(hold_to_gap.get("config_snapshot"), dict) else {}
    if hold_to_gap.get("trades_analyzed"):
        avg_d = hold_to_gap.get("avg_delta_d1_vs_actual")
        if avg_d is not None and float(avg_d) > 1.0:
            recs.append(
                f"Контрфакт d+1 open в среднем +{float(avg_d):.2f} п.п. к факту — проверьте EOD/hold политику ({cfg.get('note_ru', '')})."
            )
        if int(hold_to_gap.get("simulated_policy_better_count") or 0) >= 3:
            recs.append(
                f"Симуляция текущей политики лучше факта в {hold_to_gap.get('simulated_policy_better_count')} сделках — "
                "имеет смысл держать bundle или продлить observe."
            )
    if experiment.get("active") and experiment.get("status") == "ready_for_review":
        recs.append("Active experiment ready_for_review — сравните baseline vs observations и решите keep/rollback.")
    elif experiment.get("active") and experiment.get("status") == "pending_effect":
        recs.append("Есть pending experiment — запустите observe (cron или tuning/observe) после ≥8 новых сделок.")
    unknown = next((b for b in by_bundle if b.get("bundle_key") == "unknown"), None)
    if unknown and int(unknown.get("n") or 0) > 0:
        recs.append(
            f"{unknown.get('n')} сделок без active_bundle_id в context — после деплоя новые BUY будут штамповаться."
        )
    if not recs:
        recs.append("Явных сигналов для смены bundle нет — продолжайте observe и накопление телеметрии.")
    return recs


def build_weekly_game5m_tactic_review(
    *,
    days: int = 7,
    strategy: str = "GAME_5M",
    ledger_raw: str = "",
    limit_hold_to_gap: int = 30,
) -> Dict[str, Any]:
    from report_generator import get_engine
    from services.game5m_hold_to_gap_backtest import build_hold_to_gap_backtest
    from services.trade_effectiveness_analyzer import (
        _build_decision_stack_shadow_diff,
        _estimate_trade_effects,
        _load_closed_trades,
        _prepare_ohlc_cache,
        analyze_trade_effectiveness,
    )

    days = max(1, min(int(days), 30))
    su = (strategy or "GAME_5M").strip().upper()
    closed = _load_closed_trades(days=days, strategy_name=su)
    tickers = sorted({str(getattr(t, "ticker", "")) for t in closed if getattr(t, "ticker", None)})
    cache = _prepare_ohlc_cache(tickers=tickers, days=days + 5)
    effects = _estimate_trade_effects(closed, cache)

    engine = get_engine()
    hold_to_gap = build_hold_to_gap_backtest(
        closed,
        effects,
        cache,
        engine=engine,
        limit=limit_hold_to_gap,
    )
    mirror = _build_decision_stack_shadow_diff(su, closed, effects, limit=15)

    analyzer_light = analyze_trade_effectiveness(
        days=days,
        strategy=su,
        use_llm=False,
        light=True,
        sections=("summary", "exits"),
    )
    summary = analyzer_light.get("summary") if isinstance(analyzer_light.get("summary"), dict) else {}
    te_early = analyzer_light.get("time_exit_early_review") if isinstance(analyzer_light.get("time_exit_early_review"), dict) else {}

    ledger = load_ledger(ledger_raw)
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    observe_days = int(active.get("observe_days") or 5) if active else 5
    experiment = _experiment_review(active, observe_days=observe_days)
    active_tactic = get_active_tactic_snapshot(ledger_raw=ledger_raw)
    by_bundle = _aggregate_trades_by_bundle(closed, effects)

    multiday_gates = analyzer_light.get("multiday_lr_gates_arbiter")
    if not isinstance(multiday_gates, dict):
        try:
            from services.analyzer_ml_arbiter import build_multiday_lr_gates_arbiter

            multiday_gates = build_multiday_lr_gates_arbiter(
                analyzer_light,
                strategy=su,
                closed_trades=closed,
                effects=effects,
            )
        except Exception as e:
            multiday_gates = {"mode": "error", "note": str(e)[:200]}

    recs = _recommendations_ru(
        hold_to_gap=hold_to_gap,
        experiment=experiment,
        active_tactic=active_tactic,
        by_bundle=by_bundle,
    )

    postmortem_recs: List[str] = []
    postmortem_tactics: Dict[str, Any] = {}
    try:
        from services.game5m_trade_postmortem import load_tactics_aggregate, recommendations_ru_from_tactics

        postmortem_tactics = load_tactics_aggregate()
        if postmortem_tactics:
            postmortem_recs = recommendations_ru_from_tactics(postmortem_tactics)
            recs = postmortem_recs + [r for r in recs if r not in postmortem_recs]
    except Exception:
        pass

    return {
        "mode": "weekly_game5m_tactic_review",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_days": days,
        "strategy": su,
        "closed_trades": len(closed),
        "active_tactic": active_tactic,
        "experiment_review": experiment,
        "summary": summary,
        "hold_to_gap_counterfactual": hold_to_gap,
        "decision_stack_shadow_diff": mirror,
        "time_exit_early_review": {
            "count": te_early.get("count"),
            "premature_whipsaw_1h_count": te_early.get("premature_whipsaw_1h_count"),
            "by_exit_detail": te_early.get("by_exit_detail"),
        },
        "multiday_lr_gates_arbiter": multiday_gates,
        "trades_by_bundle": by_bundle,
        "recommendations_ru": recs,
        "postmortem_tactics": postmortem_tactics,
        "postmortem_recommendations_ru": postmortem_recs,
        "tactic_context_keys": list(TACTIC_CONTEXT_KEYS),
        "ledger_path": str(ledger_raw or "default"),
    }


def write_weekly_review(report: Dict[str, Any], path=None) -> str:
    import json
    from pathlib import Path

    p = Path(path) if path else default_weekly_review_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(p)

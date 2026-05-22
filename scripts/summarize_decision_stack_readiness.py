#!/usr/bin/env python3
"""Print a compact decision_stack/readiness summary from analyzer JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def _d(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _lines(values: Iterable[Any], *, limit: int = 5) -> list[str]:
    out: list[str] = []
    for item in values:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _print_block(title: str, lines: Iterable[str]) -> None:
    print(f"\n## {title}")
    emitted = False
    for line in lines:
        emitted = True
        print(f"- {line}")
    if not emitted:
        print("- no data")


def _fmt_num(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def summarize(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = _d(data, "meta")
    summary = _d(data, "summary")
    ml = _d(data, "ml_production_arbiter")
    verdicts = _d(ml, "verdicts")
    llm = _d(data, "llm")
    llm_analysis = _d(llm, "analysis")
    recovery = _d(data, "recovery_ml_d4a_live_review")
    multiday = _d(data, "multiday_lr_gates_arbiter")
    gap = _d(data, "game5m_gap_forecast_arbiter")
    portfolio = _d(data, "portfolio_catboost_status")
    catboost = _d(data, "game5m_catboost_status")
    auto = _d(data, "auto_config_override")

    print(f"Analyzer JSON: {path}")
    print(
        "Window: "
        f"strategy={meta.get('strategy')} days={meta.get('days')} "
        f"trades={summary.get('total')} pnl=${summary.get('sum_net_pnl_usd')} "
        f"win_rate={summary.get('win_rate_pct')}%"
    )
    if isinstance(meta.get("scope_breakdown"), dict):
        sb = meta["scope_breakdown"]
        print(f"Scope: GAME_5M={sb.get('game_5m_closed')} portfolio={sb.get('portfolio_closed')}")

    _print_block(
        "Decision Stack Readiness",
        [
            f"overall ML arbiter: {ml.get('overall_verdict', 'n/a')}",
            f"multiday ridge: {verdicts.get('multiday_ridge', 'n/a')} (gates: {verdicts.get('multiday_lr_gates', 'n/a')})",
            f"portfolio CatBoost: {verdicts.get('portfolio_catboost', 'n/a')} "
            f"(trust={portfolio.get('trust_level', 'n/a')}, rmse={portfolio.get('rmse_valid', 'n/a')})",
            f"GAME_5M CatBoost entry: {verdicts.get('catboost_entry', 'n/a')} "
            f"(enabled={catboost.get('enabled')}, trust={catboost.get('trust_level', 'n/a')})",
            f"recovery CatBoost: {verdicts.get('recovery_catboost', 'n/a')} "
            f"(live gates={recovery.get('trades_gate_ok_with_proba', 'n/a')})",
            f"gap forecast: {verdicts.get('gap_forecast', 'n/a')}",
        ],
    )
    pooled_gap_eval = _d(gap, "pooled_model_eval")
    pooled_gap_metrics = _d(pooled_gap_eval, "eval")
    gap_pool = _d(gap, "pooled")
    pm_baseline = _d(gap_pool, "premarket_baseline")
    _print_block(
        "Forecast Layer / Gap",
        [
            f"gap overall: {gap.get('overall_verdict', 'n/a')} ticker={gap.get('ticker_verdict', 'n/a')}",
            (
                "pooled ridge shadow: "
                f"mode={pooled_gap_eval.get('mode', 'n/a')} "
                f"n_eval={pooled_gap_eval.get('n_eval', 'n/a')} "
                f"MAE={pooled_gap_metrics.get('mae_pp', 'n/a')} "
                f"sign={pooled_gap_metrics.get('sign_agreement_rate', 'n/a')}"
            ),
            (
                "premarket naive baseline: "
                f"n={pm_baseline.get('n_complete', 'n/a')} "
                f"MAE={pm_baseline.get('mean_abs_error_pred_pp', 'n/a')} "
                f"sign={pm_baseline.get('sign_agreement_rate', 'n/a')}"
            ),
        ],
    )

    entry_gate = _d(multiday, "entry_gate")
    hold_gate = _d(multiday, "hold_gate")
    _print_block(
        "Apply / Shadow Recommendation",
        [
            "keep DECISION_STACK_RESOLVE_ENABLED=false until shadow diff exists on fresh deployed code",
            "portfolio_catboost can participate as readiness-gated contour; keep runtime evidence monitoring",
            (
                "multiday gates stay log_only: "
                f"entry={entry_gate.get('verdict', 'n/a')} "
                f"hold={hold_gate.get('verdict', 'n/a')}"
            ),
            (
                "recovery_ml stays readiness-gated/shadow unless live gate sample grows: "
                f"time_exit_early={recovery.get('time_exit_early_game5m_in_window', 'n/a')} "
                f"with_gate={recovery.get('trades_with_recovery_gate', 'n/a')}"
            ),
            "event_reaction stays log_only until readiness log is green",
            "GAME_5M catboost entry stays disabled/telemetry until AUC and n_valid improve",
        ],
    )

    priorities = llm_analysis.get("priorities") if isinstance(llm_analysis.get("priorities"), list) else []
    _print_block("LLM Priorities", _lines(priorities, limit=5))

    updates = auto.get("updates") if isinstance(auto.get("updates"), list) else []
    update_lines = []
    for row in updates[:8]:
        if isinstance(row, dict):
            update_lines.append(
                f"{row.get('env_key')}={_fmt_num(row.get('proposed'))} "
                f"(current={_fmt_num(row.get('current'))})"
            )
    _print_block("Config Candidates", update_lines)

    env_block: Optional[str] = auto.get("env_block") if isinstance(auto.get("env_block"), str) else None
    if env_block:
        print("\n## Env Block")
        print(env_block)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize analyzer JSON for decision_stack readiness.")
    parser.add_argument("json_path", type=Path)
    args = parser.parse_args()
    summarize(args.json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

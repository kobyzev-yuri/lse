#!/usr/bin/env python3
"""
B2 continuation ML — scheduled go/no-go review (список B).

Reads live telemetry + offline backtest from trade_effects window, writes:
  /app/logs/ml/ml_data_quality/last_game5m_b2_continuation_gonogo.json

  python scripts/run_game5m_b2_continuation_gonogo_review.py
  python scripts/run_game5m_b2_continuation_gonogo_review.py --days 30 --json-out /tmp/b2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.trade_effectiveness_analyzer import (  # noqa: E402
    _build_continuation_ml_live_review,
    _build_continuation_take_delay_backtest,
    _estimate_trade_effects,
    _load_closed_trades,
)

DEFAULT_MIN_TAKE_TELEMETRY = 8
DEFAULT_TARGET_TAKE_TELEMETRY = 15
DEFAULT_MIN_STATUS_OK_PCT = 0.80
DEFAULT_MIN_AUC_VALID = 0.55


def _default_out_path() -> Path:
    q = Path("/app/logs/ml/ml_data_quality")
    if q.is_dir():
        return q / "last_game5m_b2_continuation_gonogo.json"
    return project_root / "local/logs/ml/ml_data_quality/last_game5m_b2_continuation_gonogo.json"


def _status_ok_fraction(status_counts: Dict[str, Any]) -> Optional[float]:
    if not status_counts:
        return None
    total = sum(int(v) for v in status_counts.values())
    if total <= 0:
        return None
    ok = int(status_counts.get("ok") or 0)
    return ok / total


def evaluate_b2_gonogo_gates(
    live: Dict[str, Any],
    backtest: Dict[str, Any],
    *,
    min_take_telemetry: int = DEFAULT_MIN_TAKE_TELEMETRY,
    target_take_telemetry: int = DEFAULT_TARGET_TAKE_TELEMETRY,
    min_status_ok_pct: float = DEFAULT_MIN_STATUS_OK_PCT,
    min_auc_valid: float = DEFAULT_MIN_AUC_VALID,
) -> Dict[str, Any]:
    """Pure gate logic for tests and CLI."""
    n_telemetry = int(live.get("trades_with_continuation_ml") or 0)
    status_ok_frac = _status_ok_fraction(live.get("status_counts") or {})
    meta = backtest.get("meta_summary") if isinstance(backtest.get("meta_summary"), dict) else {}
    auc_valid = meta.get("auc_valid")
    try:
        auc_f = float(auc_valid) if auc_valid is not None else None
    except (TypeError, ValueError):
        auc_f = None

    delta_mean = backtest.get("delta_log_return_mean")
    try:
        delta_f = float(delta_mean) if delta_mean is not None else None
    except (TypeError, ValueError):
        delta_f = None

    checks: List[Dict[str, Any]] = [
        {
            "gate": "live_telemetry_count",
            "pass": n_telemetry >= min_take_telemetry,
            "value": n_telemetry,
            "threshold": f">={min_take_telemetry} (target {target_take_telemetry})",
        },
        {
            "gate": "status_ok_share",
            "pass": status_ok_frac is not None and status_ok_frac >= min_status_ok_pct,
            "value": round(status_ok_frac, 4) if status_ok_frac is not None else None,
            "threshold": f">={min_status_ok_pct:.0%}",
        },
        {
            "gate": "model_auc_valid",
            "pass": auc_f is not None and auc_f >= min_auc_valid,
            "value": auc_f,
            "threshold": f">={min_auc_valid}",
        },
        {
            "gate": "backtest_not_negative",
            "pass": delta_f is None or delta_f >= 0.0,
            "value": delta_f,
            "threshold": "delta_log_return_mean >= 0 (offline TAKE delay)",
            "optional": delta_f is None,
        },
    ]

    hard_fail = [c for c in checks if not c.get("optional") and not c["pass"]]
    if not hard_fail and n_telemetry >= target_take_telemetry:
        verdict = "go"
        rationale = (
            f"Все гейты пройдены; telemetry {n_telemetry}>={target_take_telemetry}. "
            "Можно готовить bundle GAME_5M_CONTINUATION_ML_GATE_MODE=apply (один change window)."
        )
    elif not hard_fail:
        verdict = "caution"
        rationale = (
            f"Гейты минимума пройдены ({n_telemetry} TAKE), но < {target_take_telemetry} — "
            "apply возможен с осторожностью или defer до 21.07."
        )
    elif n_telemetry < min_take_telemetry:
        verdict = "defer"
        rationale = (
            f"Мало telemetry ({n_telemetry}<{min_take_telemetry}) — перенос review, "
            "оставить log_only."
        )
    else:
        verdict = "no_go"
        rationale = "Гейты не пройдены — остаёмся log_only, правим модель/τ или ждём окно."

    return {
        "verdict": verdict,
        "rationale_ru": rationale,
        "gate_checks": checks,
        "recommended_action": {
            "go": "apply bundle: GAME_5M_CONTINUATION_ML_GATE_MODE=apply (после sign-off, один bundle)",
            "caution": "ops vote: apply log_only→apply или defer до backup review",
            "defer": "log_only; повторить прогон на backup date",
            "no_go": "log_only; не поднимать GATE_MODE=apply",
        }.get(verdict, "log_only"),
    }


def build_b2_continuation_gonogo_report(*, days: int, strategy: str = "GAME_5M") -> Dict[str, Any]:
    closed = _load_closed_trades(days, strategy)
    effects, cache = _estimate_trade_effects(closed, strategy=strategy, include_ohlc=True)
    live = _build_continuation_ml_live_review(effects)
    backtest = _build_continuation_take_delay_backtest(effects, cache, strategy=strategy)
    gates = evaluate_b2_gonogo_gates(live, backtest)
    return {
        "schema_version": "b2_gonogo_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "strategy": strategy,
        "contour": "B2_continuation_ml",
        "continuation_ml_live_review": live,
        "continuation_take_delay_backtest": backtest,
        "gonogo": gates,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 continuation ML go/no-go review")
    ap.add_argument("--days", type=int, default=30, help="Trade effects window (default 30)")
    ap.add_argument("--strategy", default="GAME_5M")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()
    days = max(7, min(int(args.days), 120))
    out_path = args.json_out or _default_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    report = build_b2_continuation_gonogo_report(days=days, strategy=args.strategy)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    g = report["gonogo"]
    print(f"B2 continuation go/no-go: {g['verdict'].upper()}")
    print(g["rationale_ru"])
    for c in g["gate_checks"]:
        mark = "OK" if c["pass"] else "FAIL"
        print(f"  [{mark}] {c['gate']}: {c.get('value')} (need {c['threshold']})")
    print(f"recommended: {g['recommended_action']}")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Live shadow report: open-path classifier vs rule labels on matured sessions."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.open_path_classifier_dataset import FEATURE_BUILDER_VERSION, LABEL_SOURCE
from services.open_path_scenario_signal import SCENARIO_SOURCE_SIGN, expected_sign_for_scenario, predict_open_path_from_json


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def round_trip_cost_log() -> float:
    bps = _cfg_float("PORTFOLIO_ML_TRANSACTION_COST_BPS", 20.0)
    return 2.0 * (bps / 10000.0)


def _sign(x: float | None, *, eps: float = 1e-9) -> int:
    if x is None or not math.isfinite(float(x)):
        return 0
    v = float(x)
    if v > eps:
        return 1
    if v < -eps:
        return -1
    return 0


def default_shadow_report_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_open_path_scenario_shadow.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_open_path_scenario_shadow.json"


def compute_open_path_trading_metric_gate(aggregate: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    min_n = _cfg_int("ML_READINESS_OPEN_PATH_SHADOW_MIN_MATURED", 80)
    min_sign = _cfg_float("ML_READINESS_OPEN_PATH_SHADOW_MIN_SIGN_ACCURACY", 0.55)
    min_class = _cfg_float("ML_READINESS_OPEN_PATH_SHADOW_MIN_CLASS_ACCURACY", 0.35)
    min_pnl = _cfg_float("ML_READINESS_OPEN_PATH_SHADOW_MIN_MEAN_PNL_LOG", 0.0)

    n_sign = int(aggregate.get("n_sign_scored") or 0)
    n_class = int(aggregate.get("n_class_scored") or 0)
    sign_acc = aggregate.get("sign_accuracy")
    class_acc = aggregate.get("class_accuracy")
    mean_pnl = aggregate.get("mean_pseudo_pnl_log")

    if int(aggregate.get("n_matured") or 0) < min_n:
        reasons.append(f"n_matured<{min_n}")
    if n_sign < min_n:
        reasons.append(f"n_sign_scored<{min_n}")
    if sign_acc is None:
        reasons.append("no_sign_accuracy")
    elif float(sign_acc) < min_sign:
        reasons.append(f"sign_accuracy<{min_sign}")
    if n_class < min_n:
        reasons.append(f"n_class_scored<{min_n}")
    if class_acc is None:
        reasons.append("no_class_accuracy")
    elif float(class_acc) < min_class:
        reasons.append(f"class_accuracy<{min_class}")
    if mean_pnl is None:
        reasons.append("no_mean_pseudo_pnl")
    elif float(mean_pnl) < min_pnl:
        reasons.append(f"mean_pseudo_pnl_log<{min_pnl}")

    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "n_sign_scored": n_sign,
        "n_class_scored": n_class,
        "sign_accuracy": sign_acc,
        "class_accuracy": class_acc,
        "mean_pseudo_pnl_log": mean_pnl,
        "advisory_only": True,
        "note": "Open-path shadow gate does not enable decision_stack apply.",
    }


def evaluate_open_path_scenario_shadow(
    engine: Engine,
    *,
    since: str = "2026-01-01",
    threshold_log: float = 0.001,
) -> dict[str, Any]:
    q = text(
        """
        SELECT
          trade_date,
          symbol,
          scenario_label,
          label_source,
          close_open_log_ret,
          features_before
        FROM game5m_open_path_labels
        WHERE label_status = 'ok'
          AND label_source = :ls
          AND features_before IS NOT NULL
          AND features_before <> '{}'::jsonb
          AND (features_before->>'feature_builder_version') = :fbv
          AND close_open_log_ret IS NOT NULL
          AND trade_date >= CAST(:since AS date)
        ORDER BY trade_date DESC, symbol
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            q,
            {"ls": LABEL_SOURCE, "fbv": FEATURE_BUILDER_VERSION, "since": since[:10]},
        ).mappings().all()

    rt_cost = round_trip_cost_log()
    detail: List[dict[str, Any]] = []
    sign_hits = sign_total = 0
    class_hits = class_total = 0
    pseudo_pnls: list[float] = []

    for r in rows:
        sym = str(r["symbol"]).strip().upper()
        actual_label = str(r["scenario_label"] or "").strip()
        actual_log = float(r["close_open_log_ret"])
        pred = predict_open_path_from_json(r.get("features_before"), symbol=sym)
        pred_scenario = pred.get("predicted_scenario")
        exp_sign = pred.get("predicted_scenario_sign")
        if exp_sign is None and pred_scenario:
            exp_sign = expected_sign_for_scenario(str(pred_scenario))

        sign_match: bool | None = None
        pseudo_pnl: float | None = None
        act_sign = _sign(actual_log, eps=threshold_log)
        if act_sign != 0 and exp_sign is not None and float(exp_sign) != 0.0:
            exp_s = _sign(float(exp_sign), eps=0.05)
            if exp_s != 0:
                sign_match = act_sign == exp_s
                sign_total += 1
                if sign_match:
                    sign_hits += 1
                direction = 1.0 if exp_s > 0 else -1.0
                pseudo_pnl = direction * actual_log - rt_cost
                pseudo_pnls.append(pseudo_pnl)

        class_match: bool | None = None
        if pred_scenario and actual_label:
            class_match = str(pred_scenario) == actual_label
            class_total += 1
            if class_match:
                class_hits += 1

        detail.append(
            {
                "symbol": sym,
                "trade_date": r["trade_date"].isoformat() if r.get("trade_date") else None,
                "actual_scenario_label": actual_label,
                "actual_close_open_log_ret": round(actual_log, 6),
                "predicted_scenario": pred_scenario,
                "predicted_scenario_proba": pred.get("predicted_scenario_proba"),
                "predicted_scenario_sign": exp_sign,
                "open_path_classifier_status": pred.get("open_path_classifier_status"),
                "sign_match": sign_match,
                "class_match": class_match,
                "pseudo_pnl_log": round(pseudo_pnl, 6) if pseudo_pnl is not None else None,
            }
        )

    agg = {
        "n_matured": len(rows),
        "n_sign_scored": sign_total,
        "sign_accuracy": round(sign_hits / sign_total, 4) if sign_total else None,
        "n_class_scored": class_total,
        "class_accuracy": round(class_hits / class_total, 4) if class_total else None,
        "mean_pseudo_pnl_log": round(sum(pseudo_pnls) / len(pseudo_pnls), 6) if pseudo_pnls else None,
        "sum_pseudo_pnl_log": round(sum(pseudo_pnls), 6) if pseudo_pnls else None,
        "round_trip_cost_log": round(rt_cost, 6),
        "transaction_cost_bps_per_leg": _cfg_float("PORTFOLIO_ML_TRANSACTION_COST_BPS", 20.0),
    }
    trading_gate = compute_open_path_trading_metric_gate(agg)

    return {
        "report_version": "open_path_scenario_shadow_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "since": since,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "scenario_source_sign_map": SCENARIO_SOURCE_SIGN,
        "aggregate": agg,
        "trading_gate": trading_gate,
        "rows": detail,
    }


def write_open_path_scenario_shadow_report(
    engine: Engine,
    *,
    project_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    report = evaluate_open_path_scenario_shadow(engine, **kwargs)
    out_path = default_shadow_report_path(project_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report

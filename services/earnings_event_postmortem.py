"""Post-mortem rows and rolling trust metrics for matured earnings events (L2.5 T_hit)."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.earnings_event_brief import load_peer_edges, load_peer_spillover_outcomes
from services.earnings_intelligence_fusion import _advisory_stance
from services.earnings_scenario_shadow import _load_matured_rows, _sign
from services.earnings_scenario_signal import expected_sign_for_scenario, predict_scenario_from_features
from services.event_reaction_catboost_signal import predict_event_reaction_from_features
from services.event_reaction_labeling import FEATURE_BUILDER_VERSION_EARNINGS, timing_from_features_before
from services.peer_spillover_signal import predict_peer_spillover

POSTMORTEM_VERSION = "earnings_postmortem_v1"
ROLLING_WINDOW_DAYS = 90


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def _ml_data_quality_dir(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality"


def default_postmortem_rows_path(project_root: Path | None = None) -> Path:
    return _ml_data_quality_dir(project_root) / "last_earnings_postmortem_rows.jsonl"


def default_trust_metrics_path(project_root: Path | None = None) -> Path:
    return _ml_data_quality_dir(project_root) / "last_earnings_trust_metrics.json"


def _rmse_bucket(pred: float | None, fact: float | None, *, threshold_log: float) -> str | None:
    if pred is None or fact is None:
        return None
    err = abs(float(pred) - float(fact))
    if err <= threshold_log:
        return "hit"
    if err <= threshold_log * 2:
        return "miss_small"
    return "miss_large"


def _peer_spillover_block(
    engine: Engine,
    *,
    source_symbol: str,
    event_date: date,
    features_before: Any,
    source_market_phase: str,
    threshold_log: float,
) -> list[dict[str, Any]]:
    peers = load_peer_edges(engine, source_ticker=source_symbol)
    if not peers:
        return []
    tickers = [str(p.get("target_ticker") or "").upper() for p in peers if p.get("target_ticker")]
    facts = {
        str(p.get("ticker") or "").upper(): p
        for p in load_peer_spillover_outcomes(
            source_event_date=event_date,
            peer_tickers=tickers[:12],
            source_market_phase=source_market_phase,
        )
    }
    out: list[dict[str, Any]] = []
    for edge in peers[:12]:
        peer = str(edge.get("target_ticker") or "").upper()
        if not peer:
            continue
        pred_out = predict_peer_spillover(
            source_symbol=source_symbol,
            peer_ticker=peer,
            features_before=features_before,
            edge_weight=float(edge.get("weight") or 0.5),
            relation_type=str(edge.get("relation_type") or "unknown"),
        )
        pred = pred_out.get("peer_forward_log_ret_5d_pred")
        try:
            pred_f = float(pred) if pred is not None else None
        except (TypeError, ValueError):
            pred_f = None
        fact_block = facts.get(peer) or {}
        fact_5d = fact_block.get("forward_log_ret_5d")
        try:
            fact_f = float(fact_5d) if fact_5d is not None else None
        except (TypeError, ValueError):
            fact_f = None
        sign_hit: bool | None = None
        if pred_f is not None and fact_f is not None:
            p_sign = _sign(pred_f, eps=threshold_log)
            f_sign = _sign(fact_f, eps=threshold_log)
            if p_sign != 0 and f_sign != 0:
                sign_hit = p_sign == f_sign
        out.append(
            {
                "peer": peer,
                "pred": round(pred_f, 6) if pred_f is not None else None,
                "fact_5d": round(fact_f, 6) if fact_f is not None else None,
                "sign_hit": sign_hit,
                "status": pred_out.get("peer_spillover_ml_status"),
            }
        )
    return out


def build_event_postmortem_row(
    engine: Engine,
    row: dict[str, Any],
    *,
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    threshold_log: float | None = None,
) -> dict[str, Any] | None:
    """One post-mortem row when source forward_log_ret_5d is present."""
    sym = str(row.get("symbol") or "").upper()
    ev_d = row.get("event_date")
    if isinstance(ev_d, str):
        ev_d = date.fromisoformat(ev_d[:10])
    if not sym or not isinstance(ev_d, date):
        return None

    outcomes = _json_obj(row.get("outcomes_after"))
    actual_5d = outcomes.get("forward_log_ret_5d")
    try:
        fact_5d = float(actual_5d) if actual_5d is not None else None
    except (TypeError, ValueError):
        fact_5d = None
    if fact_5d is None:
        return None

    thr = threshold_log if threshold_log is not None else _cfg_float("EVENT_REACTION_LABEL_THRESHOLD_LOG", 0.004)
    features = row.get("features_before")
    phase = timing_from_features_before(features)

    reg = predict_event_reaction_from_features(sym, _json_obj(features))
    reg_pred = reg.get("event_reaction_ml_expected_log_return_5d")
    try:
        reg_pred_f = float(reg_pred) if reg_pred is not None else None
    except (TypeError, ValueError):
        reg_pred_f = None

    scen = predict_scenario_from_features(sym, features, feature_builder_version=feature_builder_version)
    pred_scenario = scen.get("predicted_scenario")
    exp_sign = scen.get("predicted_scenario_sign")
    if exp_sign is None and pred_scenario:
        exp_sign = expected_sign_for_scenario(str(pred_scenario))

    fact_sign = _sign(fact_5d, eps=thr)
    pred_sign = _sign(float(exp_sign), eps=0.05) if exp_sign is not None else 0
    sign_hit: bool | None = None
    if pred_sign != 0 and fact_sign != 0:
        sign_hit = pred_sign == fact_sign

    actual_label = str(row.get("final_label") or "").strip() or None
    label_source = str(row.get("label_source") or "")
    class_hit: bool | None = None
    if pred_scenario and actual_label and label_source == "llm_scenario_v0":
        class_hit = str(pred_scenario) == actual_label

    peer_rows = _peer_spillover_block(
        engine,
        source_symbol=sym,
        event_date=ev_d,
        features_before=features,
        source_market_phase=phase,
        threshold_log=thr,
    )

    advisory = _advisory_stance(
        reg_pred=reg_pred_f,
        scenario=str(pred_scenario) if pred_scenario else None,
        scenario_sign=float(exp_sign) if exp_sign is not None else None,
        threshold_log=thr,
    )
    would_block = advisory.get("conviction") == "low" or advisory.get("alignment") == "conflict"

    return {
        "postmortem_version": POSTMORTEM_VERSION,
        "symbol": sym,
        "event_date": ev_d.isoformat(),
        "models": {
            "regression_5d": {
                "pred": round(reg_pred_f, 6) if reg_pred_f is not None else None,
                "fact": round(fact_5d, 6),
                "sign_hit": sign_hit,
                "rmse_bucket": _rmse_bucket(reg_pred_f, fact_5d, threshold_log=thr),
            },
            "scenario_sign": {
                "pred_sign": pred_sign if pred_sign else None,
                "fact_sign": fact_sign if fact_sign else None,
                "hit": sign_hit,
                "class_hit": class_hit,
                "predicted_scenario": pred_scenario,
            },
            "peer_spillover": peer_rows,
        },
        "fusion": {
            "alignment": advisory.get("alignment"),
            "conviction": advisory.get("conviction"),
            "would_have_blocked": would_block,
        },
    }


def aggregate_earnings_trust_metrics(
    rows: list[dict[str, Any]],
    *,
    window_days: int = ROLLING_WINDOW_DAYS,
) -> dict[str, Any]:
    """Rolling T_hit aggregates for earnings contours."""
    cutoff = date.today() - timedelta(days=max(1, window_days))
    recent: list[dict[str, Any]] = []
    scen_hits = scen_total = 0
    reg_hits = reg_total = 0
    peer_hits = peer_total = 0
    blocked = blocked_total = 0

    for row in rows:
        ev_s = str(row.get("event_date") or "")[:10]
        try:
            ev_d = date.fromisoformat(ev_s)
        except ValueError:
            continue
        if ev_d < cutoff:
            continue
        recent.append(row)
        models = row.get("models") or {}
        scen = models.get("scenario_sign") or {}
        if scen.get("hit") is not None:
            scen_total += 1
            if scen.get("hit"):
                scen_hits += 1
        reg = models.get("regression_5d") or {}
        if reg.get("sign_hit") is not None:
            reg_total += 1
            if reg.get("sign_hit"):
                reg_hits += 1
        for peer in models.get("peer_spillover") or []:
            if peer.get("sign_hit") is not None:
                peer_total += 1
                if peer.get("sign_hit"):
                    peer_hits += 1
        fusion = row.get("fusion") or {}
        if fusion.get("would_have_blocked") is not None:
            blocked_total += 1
            if fusion.get("would_have_blocked"):
                blocked += 1

    def _acc(h: int, t: int) -> float | None:
        return round(h / t, 4) if t else None

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rolling_window_days": window_days,
        "n_events_in_window": len(recent),
        "contours": {
            "earnings_scenario": {
                "n_matured": scen_total,
                "sign_accuracy": _acc(scen_hits, scen_total),
                "T_hit": _acc(scen_hits, scen_total),
            },
            "event_reaction": {
                "n_matured": reg_total,
                "sign_accuracy": _acc(reg_hits, reg_total),
                "T_hit": _acc(reg_hits, reg_total),
            },
            "peer_spillover": {
                "n_matured": peer_total,
                "sign_accuracy": _acc(peer_hits, peer_total),
                "T_hit": _acc(peer_hits, peer_total),
            },
        },
        "fusion_blocked_rate": round(blocked / blocked_total, 4) if blocked_total else None,
        "recent_events": sorted(recent, key=lambda r: r.get("event_date") or "", reverse=True)[:5],
    }


def refresh_earnings_postmortem(
    engine: Engine,
    *,
    project_root: Path | None = None,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: str = "2026-01-01",
) -> dict[str, Any]:
    """Rebuild JSONL post-mortem rows and rolling trust metrics."""
    matured = _load_matured_rows(
        engine,
        dataset_version=dataset_version,
        feature_builder_version=feature_builder_version,
        since=since,
    )
    postmortem_rows: list[dict[str, Any]] = []
    for row in matured:
        built = build_event_postmortem_row(
            engine,
            row,
            feature_builder_version=feature_builder_version,
        )
        if built:
            postmortem_rows.append(built)

    rows_path = default_postmortem_rows_path(project_root)
    metrics_path = default_trust_metrics_path(project_root)
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    with rows_path.open("w", encoding="utf-8") as fh:
        for row in postmortem_rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    metrics = aggregate_earnings_trust_metrics(postmortem_rows)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {
        "n_postmortem_rows": len(postmortem_rows),
        "rows_path": str(rows_path),
        "metrics_path": str(metrics_path),
        "metrics": metrics,
    }

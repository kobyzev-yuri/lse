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

POSTMORTEM_VERSION = "earnings_postmortem_v2"
ROLLING_WINDOW_DAYS = 90
CONTEXT_BUCKET_MIN = 15


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _fact_was_bad(
    *,
    fact_5d: float,
    sign_hit: bool | None,
    rmse_bucket: str | None,
    threshold_log: float,
) -> bool:
    if rmse_bucket == "miss_large":
        return True
    if sign_hit is False and abs(fact_5d) > threshold_log:
        return True
    return fact_5d < -threshold_log * 2


def _aggregate_bucket(
    buckets: dict[str, dict[str, int]],
    key: str,
    *,
    sign_hit: bool | None = None,
    would_block: bool | None = None,
    fact_bad: bool | None = None,
    block_correct: bool | None = None,
) -> None:
    if not key:
        return
    b = buckets.setdefault(
        key,
        {
            "n": 0,
            "sign_hits": 0,
            "sign_total": 0,
            "blocked": 0,
            "blocked_total": 0,
            "fact_bad": 0,
            "block_correct": 0,
            "block_correct_total": 0,
        },
    )
    b["n"] += 1
    if sign_hit is not None:
        b["sign_total"] += 1
        if sign_hit:
            b["sign_hits"] += 1
    if would_block is not None:
        b["blocked_total"] += 1
        if would_block:
            b["blocked"] += 1
    if fact_bad:
        b["fact_bad"] += 1
    if block_correct is not None:
        b["block_correct_total"] += 1
        if block_correct:
            b["block_correct"] += 1


def _finalize_buckets(raw: dict[str, dict[str, int]], *, min_n: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, b in raw.items():
        n = int(b.get("n") or 0)
        if n < min_n:
            continue
        sign_total = int(b.get("sign_total") or 0)
        sign_hits = int(b.get("sign_hits") or 0)
        blocked_total = int(b.get("blocked_total") or 0)
        blocked = int(b.get("blocked") or 0)
        block_correct_total = int(b.get("block_correct_total") or 0)
        block_correct = int(b.get("block_correct") or 0)
        out[key] = {
            "n": n,
            "sign_accuracy": round(sign_hits / sign_total, 4) if sign_total else None,
            "T_hit": round(sign_hits / sign_total, 4) if sign_total else None,
            "blocked_rate": round(blocked / blocked_total, 4) if blocked_total else None,
            "block_precision": round(block_correct / block_correct_total, 4) if block_correct_total else None,
            "fact_bad_rate": round(int(b.get("fact_bad") or 0) / n, 4) if n else None,
        }
    return out


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
    rmse_bucket = _rmse_bucket(reg_pred_f, fact_5d, threshold_log=thr)
    fact_bad = _fact_was_bad(
        fact_5d=fact_5d,
        sign_hit=sign_hit,
        rmse_bucket=rmse_bucket,
        threshold_log=thr,
    )
    block_correct: bool | None = None
    if would_block is not None:
        block_correct = (would_block and fact_bad) or (not would_block and not fact_bad)

    return {
        "postmortem_version": POSTMORTEM_VERSION,
        "symbol": sym,
        "event_date": ev_d.isoformat(),
        "context": {
            "role": "source",
            "scenario_class": str(pred_scenario) if pred_scenario else None,
            "alignment": advisory.get("alignment"),
        },
        "models": {
            "regression_5d": {
                "pred": round(reg_pred_f, 6) if reg_pred_f is not None else None,
                "fact": round(fact_5d, 6),
                "sign_hit": sign_hit,
                "rmse_bucket": rmse_bucket,
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
        "fusion_outcome": {
            "fact_was_bad": fact_bad,
            "block_was_correct": block_correct,
        },
    }


def aggregate_earnings_trust_metrics(
    rows: list[dict[str, Any]],
    *,
    window_days: int = ROLLING_WINDOW_DAYS,
    context_bucket_min: int | None = None,
) -> dict[str, Any]:
    """Rolling T_hit aggregates for earnings contours + context slices."""
    min_n = context_bucket_min if context_bucket_min is not None else _cfg_int(
        "EARNINGS_TRUST_CONTEXT_BUCKET_MIN", CONTEXT_BUCKET_MIN
    )
    cutoff = date.today() - timedelta(days=max(1, window_days))
    recent: list[dict[str, Any]] = []
    scen_hits = scen_total = 0
    reg_hits = reg_total = 0
    peer_hits = peer_total = 0
    blocked = blocked_total = 0
    fusion_bad = fusion_block_correct = fusion_block_total = 0
    by_scenario: dict[str, dict[str, int]] = {}
    by_alignment: dict[str, dict[str, int]] = {}

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
        sign_hit = scen.get("hit")
        if sign_hit is not None:
            scen_total += 1
            if sign_hit:
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
        fusion_out = row.get("fusion_outcome") or {}
        would_block = fusion.get("would_have_blocked")
        if would_block is not None:
            blocked_total += 1
            if would_block:
                blocked += 1
        fact_bad = bool(fusion_out.get("fact_was_bad"))
        if fact_bad:
            fusion_bad += 1
        block_correct = fusion_out.get("block_was_correct")
        if block_correct is not None:
            fusion_block_total += 1
            if block_correct:
                fusion_block_correct += 1

        ctx = row.get("context") or {}
        scen_key = str(ctx.get("scenario_class") or scen.get("predicted_scenario") or "").strip()
        align_key = str(ctx.get("alignment") or fusion.get("alignment") or "").strip()
        _aggregate_bucket(
            by_scenario,
            scen_key,
            sign_hit=sign_hit if isinstance(sign_hit, bool) else None,
            would_block=would_block if isinstance(would_block, bool) else None,
            fact_bad=fact_bad,
            block_correct=block_correct if isinstance(block_correct, bool) else None,
        )
        _aggregate_bucket(
            by_alignment,
            align_key,
            sign_hit=sign_hit if isinstance(sign_hit, bool) else None,
            would_block=would_block if isinstance(would_block, bool) else None,
            fact_bad=fact_bad,
            block_correct=block_correct if isinstance(block_correct, bool) else None,
        )

    def _acc(h: int, t: int) -> float | None:
        return round(h / t, 4) if t else None

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rolling_window_days": window_days,
        "context_bucket_min": min_n,
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
        "fusion_quality": {
            "n": len(recent),
            "fact_bad_rate": _acc(fusion_bad, len(recent)),
            "block_precision": _acc(fusion_block_correct, fusion_block_total),
            "blocked_rate": round(blocked / blocked_total, 4) if blocked_total else None,
        },
        "by_scenario_class": _finalize_buckets(by_scenario, min_n=min_n),
        "by_alignment": _finalize_buckets(by_alignment, min_n=min_n),
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


def load_postmortem_rows(project_root: Path | None = None) -> list[dict[str, Any]]:
    path = default_postmortem_rows_path(project_root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except json.JSONDecodeError:
            continue
    return rows


def load_trust_metrics(project_root: Path | None = None) -> dict[str, Any]:
    path = default_trust_metrics_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sign_label(sign: Any) -> str:
    try:
        s = int(sign)
    except (TypeError, ValueError):
        return "—"
    if s > 0:
        return "↑ рост"
    if s < 0:
        return "↓ падение"
    return "флэт"


def _verdict_sign(hit: bool | None) -> str:
    if hit is True:
        return "знак ✓"
    if hit is False:
        return "знак ✗"
    return "знак —"


def _verdict_class(hit: bool | None) -> str:
    if hit is True:
        return "класс ✓"
    if hit is False:
        return "класс ✗"
    return "класс —"


def _verdict_rmse(bucket: str | None) -> str:
    mapping = {
        "hit": "величина ✓ (в пороге)",
        "miss_small": "промах небольшой",
        "miss_large": "промах крупный",
    }
    return mapping.get(str(bucket or "").strip(), "—")


def _verdict_fusion(row: dict[str, Any]) -> str:
    fusion = row.get("fusion") if isinstance(row.get("fusion"), dict) else {}
    outcome = row.get("fusion_outcome") if isinstance(row.get("fusion_outcome"), dict) else {}
    blocked = fusion.get("would_have_blocked")
    correct = outcome.get("block_was_correct")
    if blocked is True and correct is True:
        return "блокировка оправдана"
    if blocked is True and correct is False:
        return "блокировка ложная"
    if blocked is False and correct is True:
        return "торговать было ок"
    if blocked is False and correct is False:
        return "пропустили риск"
    if blocked is True:
        return "низкая conviction / conflict"
    return "допуск к сделке"


def format_postmortem_table_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Human-readable lines for UI table (model / tickers / pred / fact / verdict)."""
    sym = str(row.get("symbol") or "").upper()
    models = row.get("models") if isinstance(row.get("models"), dict) else {}
    lines: list[dict[str, Any]] = []

    reg = models.get("regression_5d") or {}
    if reg:
        parts = [_verdict_sign(reg.get("sign_hit")), _verdict_rmse(reg.get("rmse_bucket"))]
        lines.append(
            {
                "model": "Regression 5d",
                "tickers": sym,
                "prediction": reg.get("pred"),
                "fact_5d": reg.get("fact"),
                "verdict_ru": " · ".join(p for p in parts if p and p != "—"),
                "verdict_short": _verdict_sign(reg.get("sign_hit")),
            }
        )

    scen = models.get("scenario_sign") or {}
    if scen:
        pred_sc = scen.get("predicted_scenario") or "—"
        pred_sign = scen.get("pred_sign")
        parts = [_verdict_sign(scen.get("hit")), _verdict_class(scen.get("class_hit"))]
        lines.append(
            {
                "model": "Scenario classifier",
                "tickers": sym,
                "prediction": f"{pred_sc} ({_sign_label(pred_sign)})",
                "fact_5d": scen.get("fact"),
                "verdict_ru": " · ".join(p for p in parts if p and p != "—"),
                "verdict_short": _verdict_sign(scen.get("hit")),
            }
        )

    for peer in models.get("peer_spillover") or []:
        if not isinstance(peer, dict):
            continue
        peer_sym = str(peer.get("peer") or "").upper() or "—"
        fact = peer.get("fact_5d")
        lines.append(
            {
                "model": "Peer spillover ML",
                "tickers": peer_sym,
                "prediction": peer.get("pred"),
                "fact_5d": fact,
                "verdict_ru": _verdict_sign(peer.get("sign_hit")) if fact is not None else "5d ещё нет",
                "verdict_short": _verdict_sign(peer.get("sign_hit")),
            }
        )

    fusion = row.get("fusion") if isinstance(row.get("fusion"), dict) else {}
    if fusion:
        align = fusion.get("alignment") or "—"
        conv = fusion.get("conviction") or "—"
        lines.append(
            {
                "model": "Fusion advisory",
                "tickers": sym,
                "prediction": f"{conv} · {align}",
                "fact_5d": None,
                "verdict_ru": _verdict_fusion(row),
                "verdict_short": "block" if fusion.get("would_have_blocked") else "allow",
            }
        )
    return lines


def find_postmortem_row(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    event_date: str,
) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    ev = str(event_date or "").strip()[:10]
    if not sym or not ev:
        return None
    for row in rows:
        if str(row.get("symbol") or "").upper() == sym and str(row.get("event_date") or "")[:10] == ev:
            return row
    return None


def get_event_postmortem_payload(
    engine: Engine,
    *,
    symbol: str,
    event_date: date,
    project_root: Path | None = None,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: str = "2026-01-01",
) -> dict[str, Any]:
    """Post-mortem for one earnings event (matured 5d only)."""
    sym = str(symbol or "").strip().upper()
    ev_iso = event_date.isoformat()
    cached = find_postmortem_row(load_postmortem_rows(project_root), symbol=sym, event_date=ev_iso)
    row = cached
    if row is None:
        matured = _load_matured_rows(
            engine,
            dataset_version=dataset_version,
            feature_builder_version=feature_builder_version,
            since=since,
        )
        for raw in matured:
            if str(raw.get("symbol") or "").upper() != sym:
                continue
            raw_d = raw.get("event_date")
            if isinstance(raw_d, str):
                raw_d = date.fromisoformat(raw_d[:10])
            if raw_d != event_date:
                continue
            row = build_event_postmortem_row(
                engine,
                raw,
                feature_builder_version=feature_builder_version,
            )
            break

    trust_metrics = load_trust_metrics(project_root)
    glossary = {
        "sign_hit": "Совпадение знака: прогноз и факт 5d в одну сторону (рост/падение).",
        "class_hit": "Совпадение класса сценария с LLM-меткой (final_label).",
        "rmse_bucket": "Точность величины regression 5d: hit / miss_small / miss_large.",
        "fusion_block": "Fusion с низкой conviction или conflict → сделку бы не открывали.",
        "immature": "Post-mortem появляется после созревания forward 5d (~5 торг. дней после отчёта).",
    }

    if row is None:
        return {
            "status": "immature",
            "symbol": sym,
            "event_date": ev_iso,
            "reason_ru": "5d forward return ещё не созрел — post-mortem будет после ~5 торг. дней.",
            "glossary": glossary,
            "trust_metrics_summary": trust_metrics.get("contours"),
            "table_rows": [],
        }

    table_rows = format_postmortem_table_rows(row)
    ctx = row.get("context") if isinstance(row.get("context"), dict) else {}
    return {
        "status": "ok",
        "symbol": sym,
        "event_date": ev_iso,
        "postmortem_version": row.get("postmortem_version"),
        "context": ctx,
        "models": row.get("models"),
        "fusion": row.get("fusion"),
        "fusion_outcome": row.get("fusion_outcome"),
        "table_rows": table_rows,
        "glossary": glossary,
        "trust_metrics_summary": trust_metrics.get("contours"),
        "rolling": {
            "n_events_in_window": trust_metrics.get("n_events_in_window"),
            "by_scenario_class": trust_metrics.get("by_scenario_class"),
            "by_alignment": trust_metrics.get("by_alignment"),
            "fusion_quality": trust_metrics.get("fusion_quality"),
        },
    }

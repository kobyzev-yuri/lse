"""Unified Trust Arbiter (L2.5): one trust scale for operator Telegram and decision_stack."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from services.decision_stack._types import readiness_from_latest_report, stack_readiness, weight_for_readiness
from services.earnings_event_freshness import is_telegram_eligible_event
from services.premarket_open_gap_forecast import load_gap_forecast_metrics

ARBITER_VERSION = "trust_v1"

# n_min and T_hit apply thresholds from docs/DECISION_TRUST_ARBITER.md §3
CONTOUR_TRUST_SPECS: dict[str, dict[str, Any]] = {
    "multiday_lr": {
        "surface": "GAME_5M",
        "display": "multiday_lr",
        "n_min": 200,
        "apply_t_hit": 0.52,
        "weights": (0.15, 0.25, 0.45, 0.15),
        "gate_key": "DECISION_STACK_MULTIDAY_LR_GATE_MODE",
    },
    "catboost_entry_5m": {
        "surface": "GAME_5M",
        "display": "catboost_entry",
        "n_min": 80,
        "apply_t_hit": 0.55,
        "weights": (0.2, 0.35, 0.35, 0.1),
        "gate_key": "DECISION_STACK_CATBOOST_ENTRY_5M_GATE_MODE",
    },
    "catboost_entry_bar_v2": {
        "surface": "GAME_5M",
        "display": "catboost_entry_bar_v2",
        "n_min": 80,
        "dataset_n_min": 5000,
        "apply_t_hit": 0.55,
        "weights": (0.2, 0.35, 0.35, 0.1),
        "shadow_only": True,
    },
    "gap_forecast": {
        "surface": "GAME_5M",
        "display": "gap_forecast",
        "n_min": 30,
        "apply_t_hit": 0.50,
        "weights": (0.2, 0.3, 0.35, 0.15),
        "gate_key": "DECISION_STACK_GAP_FORECAST_GATE_MODE",
    },
    "recovery_ml": {
        "surface": "GAME_5M",
        "display": "recovery_ml",
        "n_min": 50,
        "apply_t_hit": 0.55,
        "weights": (0.2, 0.3, 0.35, 0.15),
    },
    "portfolio_catboost": {
        "surface": "PORTFOLIO",
        "display": "portfolio_cb",
        "n_min": 40,
        "apply_t_hit": 0.55,
        "weights": (0.15, 0.45, 0.3, 0.1),
    },
    "event_reaction": {
        "surface": "EARNINGS",
        "display": "regression 5d",
        "n_min": 50,
        "apply_t_hit": 0.55,
        "weights": (0.2, 0.25, 0.4, 0.15),
    },
    "earnings_scenario": {
        "surface": "EARNINGS",
        "display": "scenario shadow",
        "n_min": 50,
        "apply_t_hit": 0.60,
        "weights": (0.15, 0.25, 0.45, 0.15),
    },
    "peer_spillover": {
        "surface": "EARNINGS",
        "display": "peer spillover",
        "n_min": 80,
        "apply_t_hit": 0.55,
        "weights": (0.2, 0.2, 0.45, 0.15),
    },
}


def _ml_data_quality_dir(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality"


def default_trust_arbiter_path(project_root: Path | None = None) -> Path:
    return _ml_data_quality_dir(project_root) / "last_unified_trust_arbiter.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_multiday_wf_artifact(project_root: Path | None = None) -> dict[str, Any]:
    """Weekly cron artifact: last_multiday_wf_game5m.json."""
    q_dir = _ml_data_quality_dir(project_root)
    return _load_json(q_dir / "last_multiday_wf_game5m.json")


def multiday_lr_reality_from_wf_artifact(wf: dict[str, Any]) -> dict[str, Any]:
    """Map WF JSON to multiday_lr_reality_check-like block for trust arbiter."""
    if not wf:
        return {}
    pooled_raw = wf.get("v3nm_pooled") or wf.get("v2_pooled") or {}
    pooled_by_horizon: dict[str, Any] = {}
    for h in (1, 2, 3):
        b = pooled_raw.get(str(h)) if isinstance(pooled_raw, dict) else {}
        if not isinstance(b, dict):
            b = {}
        pooled_by_horizon[str(h)] = {
            "mean_rmse_oos_log_across_tickers": b.get("mean_rmse_oos_log_across_tickers") or b.get("rmse_log"),
            "mean_sign_accuracy": b.get("mean_sign_accuracy") or b.get("sign_accuracy") or b.get("sign"),
            "n_points_sum": b.get("n_points_sum") or b.get("n"),
        }
    if not any((pooled_by_horizon.get(str(h)) or {}).get("n_points_sum") for h in (1, 2, 3)):
        return {}
    return {
        "mode": "ok",
        "source": "last_multiday_wf_game5m.json",
        "generated_at_utc": wf.get("generated_at_utc"),
        "walkforward_production_verdict": wf.get("verdict"),
        "walkforward_verdict_rationale_ru": wf.get("rationale_ru"),
        "pooled_by_horizon": pooled_by_horizon,
        "active_feature_set": wf.get("live_feature_set"),
    }


def resolve_multiday_lr_reality_check(
    report: dict[str, Any] | None,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Prefer live analyzer report; fallback to weekly WF artifact."""
    mlr = (report or {}).get("multiday_lr_reality_check") or {}
    if mlr.get("mode") == "ok":
        return mlr
    converted = multiday_lr_reality_from_wf_artifact(load_multiday_wf_artifact(project_root))
    if converted.get("mode") == "ok":
        return converted
    return mlr


def _t_ctx_from_earnings_trust(earnings_trust: dict[str, Any]) -> float:
    fq = earnings_trust.get("fusion_quality") or {}
    prec = fq.get("block_precision")
    if prec is not None:
        return min(1.0, max(0.2, float(prec)))
    return 0.5


def _latest_ml_train_readiness(project_root: Path | None) -> dict[str, Any]:
    paths = (
        Path("/app/logs/ml/logs/ml_train_readiness.jsonl"),
        (project_root or Path(__file__).resolve().parents[1]) / "local" / "logs" / "ml_train_readiness.jsonl",
    )
    for p in paths:
        if not p.is_file():
            continue
        try:
            last = ""
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s:
                        last = s
            if last:
                row = json.loads(last)
                return row if isinstance(row, dict) else {}
        except Exception:
            continue
    return {}


def trust_label_from_score(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    if score >= 0.25:
        return "low"
    return "insufficient"


def recommended_gate_mode(trust_label: str, *, l2_ready: bool | None) -> str:
    if trust_label == "insufficient":
        return "none"
    if trust_label == "low":
        return "log_only"
    if trust_label == "medium":
        return "caution" if l2_ready else "log_only"
    if trust_label == "high" and l2_ready:
        return "apply"
    return "log_only"


def _t_model_from_readiness(contour_id: str) -> float:
    r = readiness_from_latest_report(contour_id) or stack_readiness(contour_id)
    if r == "production":
        return 0.85
    if r == "caution":
        return 0.55
    return 0.25


def _t_data(n_matured: int, n_min: int) -> float:
    if n_min <= 0:
        return 0.0
    return min(1.0, n_matured / n_min)


def _compute_trust_score(
    *,
    t_data: float,
    t_model: float,
    t_hit: float | None,
    t_ctx: float,
    weights: tuple[float, float, float, float],
    n_matured: int,
    n_min: int,
) -> tuple[float, dict[str, Any]]:
    w_d, w_m, w_h, w_c = weights
    hit_insufficient = n_matured < n_min or t_hit is None
    t_hit_eff = 0.35 if hit_insufficient else float(t_hit)
    score = w_d * t_data + w_m * t_model + w_h * t_hit_eff + w_c * t_ctx
    if hit_insufficient and score > 0.74:
        score = 0.74
    components = {
        "T_data": round(t_data, 4),
        "T_model": round(t_model, 4),
        "T_hit": round(t_hit_eff, 4) if t_hit is not None else None,
        "T_ctx": round(t_ctx, 4),
        "T_hit_insufficient": hit_insufficient,
    }
    return round(score, 4), components


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{100.0 * float(x):.0f}%"


def _contour_from_earnings_metrics(
    contour_id: str,
    spec: dict[str, Any],
    earnings_trust: dict[str, Any],
    shadow: dict[str, Any],
) -> dict[str, Any]:
    contours = earnings_trust.get("contours") or {}
    block = contours.get(contour_id) or {}
    n_matured = int(block.get("n_matured") or 0)
    if contour_id == "earnings_scenario" and n_matured == 0:
        agg = shadow.get("aggregate") or {}
        n_matured = int(agg.get("n_sign_scored") or agg.get("n_matured") or 0)
        t_hit = agg.get("sign_accuracy")
    else:
        t_hit = block.get("T_hit") or block.get("sign_accuracy")

    n_min = int(spec["n_min"])
    t_data = _t_data(n_matured, n_min)
    t_model = _t_model_from_readiness(contour_id)
    t_ctx = _t_ctx_from_earnings_trust(earnings_trust)
    if earnings_trust.get("degradation", {}).get("degrading"):
        t_ctx = min(t_ctx, 0.45)
    trust_score, components = _compute_trust_score(
        t_data=t_data,
        t_model=t_model,
        t_hit=float(t_hit) if t_hit is not None else None,
        t_ctx=t_ctx,
        weights=tuple(spec["weights"]),
        n_matured=n_matured,
        n_min=n_min,
    )
    label = trust_label_from_score(trust_score)
    l2_ready = readiness_from_latest_report(contour_id) == "production"
    gate = recommended_gate_mode(label, l2_ready=l2_ready)
    hit_note = f"n={n_matured} sign {_pct(t_hit)}" if t_hit is not None else f"n={n_matured} scored"
    return {
        "contour_id": contour_id,
        "trust_score": trust_score,
        "trust_label": label,
        **components,
        "recommended_gate_mode": gate,
        "n_matured": n_matured,
        "conclusion_ru": f"{spec['display']}: {label} {trust_score:.2f}, {gate}, {hit_note}",
    }


def _contour_from_multiday(spec: dict[str, Any], mlr: dict[str, Any]) -> dict[str, Any]:
    ph = mlr.get("pooled_by_horizon") or {}
    b1 = ph.get("1") if isinstance(ph, dict) else {}
    n_pts = int((b1 or {}).get("n_points_sum") or 0) if isinstance(b1, dict) else 0
    sign_acc = (b1 or {}).get("mean_sign_accuracy") if isinstance(b1, dict) else None
    wf_verdict = str(mlr.get("walkforward_production_verdict") or "caution")
    n_min = int(spec["n_min"])
    t_data = _t_data(n_pts, n_min)
    t_model = 0.85 if wf_verdict == "ready" else 0.55 if wf_verdict == "caution" else 0.3
    t_hit = float(sign_acc) if sign_acc is not None else None
    trust_score, components = _compute_trust_score(
        t_data=t_data,
        t_model=t_model,
        t_hit=t_hit,
        t_ctx=0.5,
        weights=tuple(spec["weights"]),
        n_matured=n_pts,
        n_min=n_min,
    )
    label = trust_label_from_score(trust_score)
    l2_ready = wf_verdict == "ready"
    gate = recommended_gate_mode(label, l2_ready=l2_ready)
    return {
        "contour_id": "multiday_lr",
        "trust_score": trust_score,
        "trust_label": label,
        **components,
        "recommended_gate_mode": gate,
        "n_matured": n_pts,
        "multiday_source": mlr.get("source") or "analyzer_report",
        "conclusion_ru": f"Multiday ridge: WF {wf_verdict}, sign 1d {_pct(t_hit)}, {gate}",
    }


def _contour_from_gap_forecast(spec: dict[str, Any], gap_metrics: dict[str, Any] | None) -> dict[str, Any]:
    gm = gap_metrics or {}
    beat = gm.get("ml_beats_baseline_pct_days") or gm.get("beat_baseline_share")
    n_days = int(gm.get("n_days") or gm.get("rolling_n_days") or 0)
    n_min = int(spec["n_min"])
    t_data = _t_data(n_days, n_min)
    t_model = 0.55 if gm.get("gate_ready") else 0.4
    t_hit = float(beat) if beat is not None else None
    trust_score, components = _compute_trust_score(
        t_data=t_data,
        t_model=t_model,
        t_hit=t_hit,
        t_ctx=0.45,
        weights=tuple(spec["weights"]),
        n_matured=n_days,
        n_min=n_min,
    )
    label = trust_label_from_score(trust_score)
    gate = recommended_gate_mode(label, l2_ready=bool(gm.get("gate_ready")))
    note = "PM baseline лучше ML" if t_hit is not None and t_hit < 0.5 else f"beat baseline {_pct(t_hit)}"
    return {
        "contour_id": "gap_forecast",
        "trust_score": trust_score,
        "trust_label": label,
        **components,
        "recommended_gate_mode": gate,
        "n_matured": n_days,
        "conclusion_ru": f"Gap forecast: {label} {trust_score:.2f}, {gate}, {note}",
    }


def _default_entry_bar_v2_metrics_paths(project_root: Path | None = None) -> tuple[Path, Path, Path]:
    """Returns (train_metrics, meta, dataset_stats)."""
    root = project_root or Path(__file__).resolve().parents[1]
    if Path("/app/logs").exists():
        base = Path("/app/logs/ml")
        q = Path("/app/logs/ml/ml_data_quality")
    else:
        base = root / "local" / "logs" / "ml"
        q = root / "local" / "logs" / "ml_data_quality"
    return (
        q / "last_game5m_entry_bar_v2_train_metrics.json",
        base / "models" / "game5m_entry_catboost_v2.meta.json",
        base / "datasets" / "game5m_entry_bar_dataset_stats.json",
    )


def _load_entry_bar_v2_metrics(project_root: Path | None = None) -> dict[str, Any]:
    train_path, meta_path, stats_path = _default_entry_bar_v2_metrics_paths(project_root)
    alt_train = train_path.parent.parent / "datasets" / "game5m_entry_bar_v2_train.json"
    data: dict[str, Any] = {}
    for p in (train_path, alt_train, meta_path):
        if not p.is_file():
            continue
        block = _load_json(p)
        if block:
            data.update(block)
    stats = _load_json(stats_path)
    if stats:
        data["dataset_stats"] = stats
        if stats.get("n_rows") is not None:
            data.setdefault("dataset_n_rows", stats.get("n_rows"))
    data["train_metrics_path"] = str(train_path)
    data["meta_path"] = str(meta_path)
    data["dataset_stats_path"] = str(stats_path)
    cbm_path = meta_path.with_name("game5m_entry_catboost_v2.cbm")
    data["model_file_exists"] = cbm_path.is_file()
    return data


def _contour_from_entry_bar_v2(spec: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    n_valid = int(metrics.get("n_valid") or 0)
    n_rows = int(metrics.get("dataset_n_rows") or (metrics.get("dataset_stats") or {}).get("n_rows") or 0)
    auc_raw = metrics.get("auc_valid")
    try:
        auc_f = float(auc_raw) if auc_raw is not None else None
    except (TypeError, ValueError):
        auc_f = None
    n_min = int(spec["n_min"])
    dataset_n_min = int(spec.get("dataset_n_min") or 5000)
    t_data = max(_t_data(n_valid, n_min), _t_data(n_rows, dataset_n_min))
    t_model = 0.45 if metrics.get("model_file_exists") else 0.25
    t_hit = auc_f
    trust_score, components = _compute_trust_score(
        t_data=t_data,
        t_model=t_model,
        t_hit=t_hit,
        t_ctx=0.5,
        weights=tuple(spec["weights"]),
        n_matured=n_valid,
        n_min=n_min,
    )
    label = trust_label_from_score(trust_score)
    if spec.get("shadow_only"):
        gate = "log_only"
    else:
        gate = recommended_gate_mode(label, l2_ready=False)
    if auc_f is not None and auc_f < float(spec.get("apply_t_hit") or 0.55):
        gate = "log_only"
    auc_note = f"AUC valid {auc_f:.2f}" if auc_f is not None else "AUC n/a"
    ds_note = f"dataset {n_rows} rows" if n_rows else "dataset n/a"
    return {
        "contour_id": "catboost_entry_bar_v2",
        "trust_score": trust_score,
        "trust_label": label,
        **components,
        "recommended_gate_mode": gate,
        "n_matured": n_valid,
        "dataset_n_rows": n_rows,
        "shadow_only": True,
        "conclusion_ru": f"CatBoost entry bar v2 (shadow): {label} {trust_score:.2f}, {gate}, {auc_note}, {ds_note}",
    }


def _contour_from_ml_readiness(contour_id: str, spec: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "catboost_entry_5m": ("game5m", "entry_catboost"),
        "catboost_entry_bar_v2": ("entry_bar_v2",),
        "portfolio_catboost": ("portfolio",),
        "recovery_ml": ("recovery",),
        "event_reaction": ("event_reaction",),
    }
    block: dict[str, Any] = {}
    for key in aliases.get(contour_id, (contour_id,)):
        raw = readiness.get(key)
        if isinstance(raw, dict):
            block = raw
            break
    gate = block.get("gate") if isinstance(block.get("gate"), dict) else block
    n_train = int(gate.get("n_train") or gate.get("n") or 0) if isinstance(gate, dict) else 0
    auc = gate.get("auc_valid") or gate.get("auc") if isinstance(gate, dict) else None
    n_min = int(spec["n_min"])
    t_data = _t_data(n_train, n_min)
    t_model = 0.85 if isinstance(gate, dict) and gate.get("ready") is True else 0.5
    t_hit = float(auc) if auc is not None and contour_id == "catboost_entry_5m" else None
    trust_score, components = _compute_trust_score(
        t_data=t_data,
        t_model=t_model,
        t_hit=t_hit,
        t_ctx=0.5,
        weights=tuple(spec["weights"]),
        n_matured=n_train,
        n_min=n_min,
    )
    label = trust_label_from_score(trust_score)
    l2_ready = isinstance(gate, dict) and gate.get("ready") is True
    gate_mode = recommended_gate_mode(label, l2_ready=l2_ready)
    auc_note = f"AUC {float(auc):.2f}" if auc is not None else f"n={n_train}"
    return {
        "contour_id": contour_id,
        "trust_score": trust_score,
        "trust_label": label,
        **components,
        "recommended_gate_mode": gate_mode,
        "n_matured": n_train,
        "conclusion_ru": f"{spec['display']}: {label} {trust_score:.2f}, {gate_mode}, {auc_note}",
    }


def _data_volume_ru(n_matured: int, n_min: int, *, unit: str = "") -> str:
    unit_s = f" {unit}".strip()
    if n_min <= 0:
        return ""
    if n_matured >= n_min:
        return f"Данных достаточно: {n_matured}/{n_min}{unit_s} ✓"
    need = max(0, n_min - n_matured)
    return f"Мало данных: {n_matured}/{n_min}{unit_s} — нужно ещё ≥{need} (порог {n_min})"


def _gate_mode_ru(gate: str) -> str:
    mapping = {
        "apply": "влияет на решение",
        "caution": "опираемся, но без максимума доверия",
        "log_only": "только telemetry — на сделки не меняет",
        "none": "выключен",
    }
    return mapping.get(str(gate or "").strip(), str(gate or "?"))


_CONTOUR_DIGEST_META: dict[str, dict[str, str]] = {
    "multiday_lr": {
        "title": "Multiday ridge",
        "role": "главный ML-фильтр 1–3d (вход и multiday hold)",
        "unit": "point-days",
        "apply_note": "На prod legacy: entry/hold apply.",
    },
    "catboost_entry_5m": {
        "title": "CatBoost вход 5m (v1 trade)",
        "role": "фильтр «входить в эту 5m-сделку или нет» — prod trade-based",
        "unit": "сделок с context",
        "apply_note": "Для apply: high trust + ≥80 сделок + AUC ≥0.55.",
    },
    "catboost_entry_bar_v2": {
        "title": "CatBoost вход bar v2 (shadow)",
        "role": "bar-level + triple barrier; telemetry catboost_entry_proba_good_v2",
        "unit": "valid rows",
        "apply_note": "Shadow only: prod v1 без изменений до AUC≥0.55 и sign-off.",
    },
    "gap_forecast": {
        "title": "Gap forecast",
        "role": "ML-прогноз open gap vs premarket baseline",
        "unit": "дней rolling",
        "apply_note": "Вход по premarket_gap_baseline, не по ML.",
    },
    "portfolio_catboost": {
        "title": "Portfolio CatBoost",
        "role": "ML по закрытым сделкам портфеля",
        "unit": "сделок",
        "apply_note": "",
    },
    "earnings_scenario": {
        "title": "Scenario shadow",
        "role": "знак сценария earnings (advisory)",
        "unit": "событий 5d",
        "apply_note": "Не блокирует GAME_5M.",
    },
    "event_reaction": {
        "title": "Regression 5d",
        "role": "регрессия реакции на earnings",
        "unit": "событий 5d",
        "apply_note": "Не блокирует GAME_5M.",
    },
    "peer_spillover": {
        "title": "Peer spillover",
        "role": "перенос сигнала на peer-тикеры",
        "unit": "пар event×peer",
        "apply_note": "Не блокирует GAME_5M.",
    },
}


def _contour_metric_ru(contour: dict[str, Any], spec: dict[str, Any]) -> str:
    cid = str(contour.get("contour_id") or "")
    conclusion = str(contour.get("conclusion_ru") or "")
    t_hit_insufficient = bool(contour.get("T_hit_insufficient"))
    t_hit = contour.get("T_hit")
    apply_t = spec.get("apply_t_hit")

    if cid == "multiday_lr":
        wf_part = ""
        if "WF ready" in conclusion:
            wf_part = "WF ready"
        elif "WF caution" in conclusion:
            wf_part = "WF caution"
        sign_part = ""
        for p in conclusion.split(","):
            if "sign 1d" in p:
                sign_part = p.strip()
                break
        metric = ", ".join(x for x in (wf_part, sign_part) if x)
        if apply_t is not None and metric:
            metric += f" (порог apply sign ≥{_pct(apply_t)})"
        return metric or conclusion.split(":", 1)[-1].strip()

    if cid == "gap_forecast":
        if "PM baseline" in conclusion:
            base = "PM baseline точнее ML"
        elif t_hit is not None and not t_hit_insufficient:
            base = f"ML лучше PM в {_pct(t_hit)} дней"
        else:
            base = "нет rolling beat-baseline"
        if apply_t is not None:
            base += f" (нужно >{_pct(apply_t)} дней)"
        return base

    if cid == "catboost_entry_5m":
        if t_hit is not None and not t_hit_insufficient:
            base = f"AUC valid {float(t_hit):.2f}"
            if apply_t is not None:
                base += f" (порог apply ≥{float(apply_t):.2f})"
            return base
        return "AUC ещё нестабилен"

    if cid == "catboost_entry_bar_v2":
        ds_rows = contour.get("dataset_n_rows")
        if t_hit is not None and not t_hit_insufficient:
            base = f"AUC valid {float(t_hit):.2f}"
            if apply_t is not None:
                base += f" (promotion ≥{float(apply_t):.2f})"
            if ds_rows:
                base += f", dataset {int(ds_rows)} rows"
            return base
        if ds_rows:
            return f"dataset {int(ds_rows)} rows, AUC ещё нестабилен"
        return "shadow / log_only"

    if t_hit is not None and not t_hit_insufficient:
        base = f"sign/метрика {_pct(t_hit)}"
        if apply_t is not None:
            base += f" (порог apply ≥{_pct(apply_t)})"
        return base
    return ""


def _contour_digest_lines(contour: dict[str, Any]) -> list[str]:
    cid = str(contour.get("contour_id") or "?")
    spec = CONTOUR_TRUST_SPECS.get(cid, {})
    meta = _CONTOUR_DIGEST_META.get(cid, {})
    title = meta.get("title") or spec.get("display") or cid
    label = str(contour.get("trust_label") or "?")
    score = float(contour.get("trust_score") or 0)
    gate = str(contour.get("recommended_gate_mode") or "?")
    n_matured = int(contour.get("n_matured") or 0)
    n_min = int(spec.get("n_min") or 0)

    lines = [f"• {title} — {label} {score:.2f}, {gate}"]
    role = meta.get("role")
    if role:
        lines.append(f"  {role}.")
    metric = _contour_metric_ru(contour, spec)
    if metric:
        lines.append(f"  {metric}.")
    lines.append(f"  {_gate_mode_ru(gate)}.")
    vol = _data_volume_ru(n_matured, n_min, unit=str(meta.get("unit") or ""))
    if vol:
        lines.append(f"  {vol}.")
    apply_note = meta.get("apply_note")
    if apply_note:
        lines.append(f"  {apply_note}")
    return lines


def format_operator_digest_ru(arbiter: dict[str, Any]) -> str:
    today = date.today().isoformat()
    lines = [f"LSE Trust · {today}", ""]
    surfaces = arbiter.get("surfaces") or {}

    game = surfaces.get("GAME_5M") or {}
    if game:
        lines.append("GAME_5M — здесь реальные сделки")
        lines.append("")
        for c in game.get("contours") or []:
            lines.extend(_contour_digest_lines(c))
            lines.append("")

    pf = surfaces.get("PORTFOLIO") or {}
    if pf:
        lines.append("PORTFOLIO — отдельная игра")
        lines.append("")
        for c in pf.get("contours") or []:
            lines.extend(_contour_digest_lines(c))
            lines.append("")

    earn = surfaces.get("EARNINGS") or {}
    if earn:
        lines.append("EARNINGS — advisory, бот не блокирует")
        lines.append("")
        for c in earn.get("contours") or []:
            lines.extend(_contour_digest_lines(c))
            lines.append("")

    ev = arbiter.get("latest_event_postmortem")
    if isinstance(ev, dict) and ev.get("symbol"):
        ev_d_raw = str(ev.get("event_date") or "")[:10]
        try:
            ev_d = date.fromisoformat(ev_d_raw)
        except ValueError:
            ev_d = None
        if not is_telegram_eligible_event(ev_d):
            ev = None
    if isinstance(ev, dict) and ev.get("symbol"):
        sym = ev.get("symbol")
        ev_d = ev.get("event_date")
        scen = (ev.get("models") or {}).get("scenario_sign") or {}
        reg = (ev.get("models") or {}).get("regression_5d") or {}
        fusion = ev.get("fusion") or {}
        hit = "✓" if scen.get("hit") else "✗"
        fact = reg.get("fact")
        fact_pct = f"{100.0 * float(fact):+.1f}%" if fact is not None else "—"
        lines.append("Последнее созревшее earnings-событие")
        lines.append(
            f"  {sym} {ev_d}: scenario sign {hit}, fact {fact_pct}, fusion {fusion.get('conviction')}"
        )
        lines.append("")

    summary = arbiter.get("summary_ru") or "См. контуры выше."
    lines.append(f"Итог: {summary}")
    lines.append("Док: docs/GAME_5M_DECISION_ARCHITECTURE.md")
    return "\n".join(lines)


def build_unified_trust_arbiter(
    *,
    project_root: Path | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = project_root or Path(__file__).resolve().parents[1]
    q_dir = _ml_data_quality_dir(root)
    shadow = _load_json(q_dir / "last_earnings_scenario_shadow.json")
    earnings_trust = _load_json(q_dir / "last_earnings_trust_metrics.json")
    readiness = _latest_ml_train_readiness(root)
    gap_metrics = load_gap_forecast_metrics()
    mlr = resolve_multiday_lr_reality_check(report, project_root=root)

    surfaces: dict[str, Any] = {}
    game_contours: list[dict[str, Any]] = []
    if mlr.get("mode") == "ok":
        game_contours.append(_contour_from_multiday(CONTOUR_TRUST_SPECS["multiday_lr"], mlr))
    else:
        game_contours.append(_contour_from_ml_readiness("multiday_lr", CONTOUR_TRUST_SPECS["multiday_lr"], readiness))
    game_contours.append(_contour_from_ml_readiness("catboost_entry_5m", CONTOUR_TRUST_SPECS["catboost_entry_5m"], readiness))
    game_contours.append(
        _contour_from_entry_bar_v2(CONTOUR_TRUST_SPECS["catboost_entry_bar_v2"], _load_entry_bar_v2_metrics(root))
    )
    game_contours.append(_contour_from_gap_forecast(CONTOUR_TRUST_SPECS["gap_forecast"], gap_metrics))
    multiday_c = game_contours[0]
    surfaces["GAME_5M"] = {
        "overall_trust": multiday_c["trust_label"],
        "contours": game_contours,
    }

    pf_contours = [_contour_from_ml_readiness("portfolio_catboost", CONTOUR_TRUST_SPECS["portfolio_catboost"], readiness)]
    surfaces["PORTFOLIO"] = {"overall_trust": pf_contours[0]["trust_label"], "contours": pf_contours}

    earn_contours = [
        _contour_from_earnings_metrics("earnings_scenario", CONTOUR_TRUST_SPECS["earnings_scenario"], earnings_trust, shadow),
        _contour_from_earnings_metrics("event_reaction", CONTOUR_TRUST_SPECS["event_reaction"], earnings_trust, shadow),
        _contour_from_earnings_metrics("peer_spillover", CONTOUR_TRUST_SPECS["peer_spillover"], earnings_trust, shadow),
    ]
    surfaces["EARNINGS"] = {
        "overall_trust": earn_contours[0]["trust_label"],
        "contours": earn_contours,
        "context_slices": {
            "by_scenario_class": earnings_trust.get("by_scenario_class") or {},
            "by_alignment": earnings_trust.get("by_alignment") or {},
            "fusion_quality": earnings_trust.get("fusion_quality") or {},
        },
    }

    weights: dict[str, float] = {}
    for surface in surfaces.values():
        for c in surface.get("contours") or []:
            cid = str(c.get("contour_id") or "")
            r = stack_readiness(cid)
            base_w = weight_for_readiness(r)
            weights[cid] = round(base_w * float(c.get("trust_score") or 0), 4)

    es = weights.get("earnings_scenario", 0.0)
    ps = weights.get("peer_spillover", 0.0)
    if es or ps:
        weights["earnings_trust"] = round(min(float(es or 0), float(ps or 1)) * 0.85, 4)

    recent = earnings_trust.get("recent_events") or []
    latest_event = None
    for row in recent:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("event_date") or "")[:10]
        try:
            ev_d = date.fromisoformat(raw)
        except ValueError:
            continue
        if is_telegram_eligible_event(ev_d):
            latest_event = row
            break

    multiday = next((c for c in game_contours if c.get("contour_id") == "multiday_lr"), {})
    summary_ru = (
        f"торговать по GAME_5M multiday ({multiday.get('trust_label', '?')}); "
        "earnings — только контекст"
    )
    if isinstance(latest_event, dict) and latest_event.get("fusion", {}).get("would_have_blocked"):
        summary_ru += f"; {latest_event.get('symbol')} — осторожность"

    arbiter = {
        "arbiter_version": ARBITER_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "multiday_lr_source": mlr.get("source") or ("analyzer_report" if mlr.get("mode") == "ok" else "fallback"),
        "surfaces": surfaces,
        "operator_digest_ru": "",
        "decision_stack_weights": weights,
        "latest_event_postmortem": latest_event,
        "summary_ru": summary_ru,
    }
    arbiter["operator_digest_ru"] = format_operator_digest_ru(arbiter)
    return arbiter


def write_unified_trust_arbiter(
    *,
    project_root: Path | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arbiter = build_unified_trust_arbiter(project_root=project_root, report=report)
    out = default_trust_arbiter_path(project_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(arbiter, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    arbiter["path"] = str(out)
    return arbiter

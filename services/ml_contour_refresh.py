"""
Unified ML contour refresh contract: data-driven retrain triggers, phases, aggregate status.

See docs/ML_UNIFIED_RETRAIN_FRAMEWORK.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config_loader import get_config_value

ML_DATA_QUALITY_SUBDIR = "ml_data_quality"


class ContourPhase(str, Enum):
    ACCUMULATING = "accumulating_data"
    QUALITY_TUNING = "quality_tuning"
    PRODUCT_READY = "product_ready"
    CONTINUOUS = "continuous_prod"


@dataclass(frozen=True)
class MlContourSpec:
    contour_id: str
    display_name_ru: str
    data_unit: str
    refresh_script: str
    train_metrics_relpath: str
    refresh_log_relpath: str
    readiness_relpath: Optional[str] = None
    product_gate_key: Optional[str] = None
    min_new_units_default: int = 5
    max_staleness_hours_default: int = 168
    poll_hours_default: int = 6
    supports_shadow: bool = False
    supports_continuous: bool = True
    config_prefix: Optional[str] = None
    legacy_env: Dict[str, str] = field(default_factory=dict)


@dataclass
class RetrainTrigger:
    contour_id: str
    should_apply_data: bool
    should_train: bool
    should_full_shadow: bool
    new_units_since_last_apply: Optional[int]
    new_units_since_last_train: Optional[int]
    staleness_hours_apply: Optional[float]
    staleness_hours_train: Optional[float]
    phase: str
    reasons: List[str]


def default_ml_data_quality_dir(project_root: Path) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml") / ML_DATA_QUALITY_SUBDIR
    return project_root / "local" / "logs" / ML_DATA_QUALITY_SUBDIR


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _cfg_bool(key: str, default: bool = False) -> bool:
    raw = (get_config_value(key, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def contour_config_prefix(spec: MlContourSpec) -> str:
    if spec.config_prefix:
        return spec.config_prefix
    return f"ML_{spec.contour_id.upper()}"


def contour_min_new_units(spec: MlContourSpec) -> int:
    prefix = contour_config_prefix(spec)
    unified = _cfg_int(f"{prefix}_RETRAIN_MIN_NEW_UNITS", spec.min_new_units_default)
    for legacy_key, legacy_default in spec.legacy_env.items():
        if "MIN" in legacy_key or "DELTA" in legacy_key:
            if get_config_value(legacy_key, ""):
                return _cfg_int(legacy_key, unified)
    return unified


def contour_max_staleness_hours(spec: MlContourSpec) -> int:
    prefix = contour_config_prefix(spec)
    return _cfg_int(f"{prefix}_RETRAIN_MAX_STALENESS_HOURS", spec.max_staleness_hours_default)


def contour_continuous_enabled(spec: MlContourSpec, *, product_ready: bool) -> bool:
    prefix = contour_config_prefix(spec)
    default = 1 if product_ready else 0
    for legacy_key in spec.legacy_env:
        if "CONTINUOUS" in legacy_key:
            return _cfg_bool(legacy_key, default)
    return _cfg_bool(f"{prefix}_CONTINUOUS_TRAIN", default)


def path_for_spec(project_root: Path, spec: MlContourSpec, relpath: str) -> Path:
    return default_ml_data_quality_dir(project_root) / relpath


def default_refresh_log_path(project_root: Path, spec: MlContourSpec) -> Path:
    return path_for_spec(project_root, spec, spec.refresh_log_relpath)


def default_aggregate_status_path(project_root: Path) -> Path:
    return default_ml_data_quality_dir(project_root) / "ml_contours_status.json"


def load_refresh_log(project_root: Path, spec: MlContourSpec) -> Dict[str, Any]:
    path = default_refresh_log_path(project_root, spec)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_refresh_log(
    project_root: Path,
    spec: MlContourSpec,
    payload: Dict[str, Any],
) -> Path:
    path = default_refresh_log_path(project_root, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "contour_id": spec.contour_id,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _hours_since(ts: Any) -> Optional[float]:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)


def resolve_phase(
    spec: MlContourSpec,
    *,
    product_ready: bool,
    dataset_ready: bool,
    train_ready: bool,
    continuous_enabled: bool,
) -> ContourPhase:
    if product_ready and continuous_enabled:
        return ContourPhase.CONTINUOUS
    if product_ready:
        return ContourPhase.PRODUCT_READY
    if dataset_ready and train_ready:
        return ContourPhase.QUALITY_TUNING
    return ContourPhase.ACCUMULATING


def evaluate_retrain_trigger(
    spec: MlContourSpec,
    refresh_log: Dict[str, Any],
    *,
    new_units_since_last_apply: Optional[int] = None,
    new_units_since_last_train: Optional[int] = None,
    product_ready: bool = False,
    dataset_ready: bool = False,
    train_ready: bool = False,
    force_full: bool = False,
    force_apply: bool = False,
) -> RetrainTrigger:
    """
    Data-driven retrain decision. Cron poll may run often; train only when this returns should_train.
    """
    min_new = contour_min_new_units(spec)
    max_stale_h = contour_max_staleness_hours(spec)
    poll_h = _cfg_int(
        f"{contour_config_prefix(spec)}_RETRAIN_POLL_HOURS",
        spec.poll_hours_default,
    )
    continuous = contour_continuous_enabled(spec, product_ready=product_ready)
    phase = resolve_phase(
        spec,
        product_ready=product_ready,
        dataset_ready=dataset_ready,
        train_ready=train_ready,
        continuous_enabled=continuous,
    )
    apply_min_new = 1 if phase == ContourPhase.CONTINUOUS else min_new
    apply_max_stale_h = poll_h if phase == ContourPhase.CONTINUOUS else max_stale_h

    last_apply = refresh_log.get("last_apply_at_utc") or refresh_log.get("finished_at_utc")
    last_train = refresh_log.get("last_train_at_utc") or (
        refresh_log.get("finished_at_utc") if refresh_log.get("train_ran") else None
    )
    stale_apply = _hours_since(last_apply)
    stale_train = _hours_since(last_train)

    nu_apply = new_units_since_last_apply
    if nu_apply is None:
        nu_apply = refresh_log.get("new_units_since_last_apply")
        if nu_apply is not None:
            nu_apply = int(nu_apply)

    nu_train = new_units_since_last_train
    if nu_train is None:
        nu_train = refresh_log.get("new_units_since_last_train")
        if nu_train is not None:
            nu_train = int(nu_train)

    reasons: List[str] = []
    delta_apply = nu_apply is not None and nu_apply >= apply_min_new
    delta_train = nu_train is not None and nu_train >= min_new
    stale_apply_hit = stale_apply is not None and stale_apply >= apply_max_stale_h
    stale_train_hit = stale_train is not None and stale_train >= max_stale_h
    no_prior_apply = last_apply is None
    no_prior_train = last_train is None

    should_apply = force_apply or force_full or no_prior_apply or delta_apply or stale_apply_hit
    if should_apply:
        if force_full:
            reasons.append("force_full")
        elif force_apply:
            reasons.append("force_apply")
        elif no_prior_apply:
            reasons.append("no_prior_apply")
        elif delta_apply:
            reasons.append(f"new_units_apply>={apply_min_new}")
        elif stale_apply_hit:
            reasons.append(f"staleness_apply>={apply_max_stale_h}h")

    train_phases = {
        ContourPhase.QUALITY_TUNING,
        ContourPhase.PRODUCT_READY,
        ContourPhase.CONTINUOUS,
    }
    should_train = False
    if force_full:
        should_train = True
        if "force_full" not in reasons:
            reasons.append("force_full")
    elif phase in train_phases and should_apply:
        if phase == ContourPhase.CONTINUOUS and continuous:
            should_train = True
            reasons.append("continuous_prod")
        elif delta_train or no_prior_train:
            should_train = True
            reasons.append(f"new_units_train>={min_new}" if delta_train else "no_prior_train")
        elif stale_train_hit:
            should_train = True
            reasons.append(f"staleness_train>={max_stale_h}h")

    should_shadow = spec.supports_shadow and (
        force_full or ((stale_train_hit) and phase != ContourPhase.ACCUMULATING)
    )

    return RetrainTrigger(
        contour_id=spec.contour_id,
        should_apply_data=should_apply,
        should_train=should_train,
        should_full_shadow=should_shadow,
        new_units_since_last_apply=nu_apply,
        new_units_since_last_train=nu_train,
        staleness_hours_apply=stale_apply,
        staleness_hours_train=stale_train,
        phase=phase.value,
        reasons=reasons,
    )


ML_CONTOUR_REGISTRY: Dict[str, MlContourSpec] = {
    "open_path": MlContourSpec(
        contour_id="open_path",
        display_name_ru="Open-path (тип поведения у open)",
        data_unit="labeled_session",
        refresh_script="scripts/run_open_path_ml_refresh.py",
        train_metrics_relpath="last_open_path_scenario_train_metrics.json",
        refresh_log_relpath="last_open_path_ml_refresh.json",
        readiness_relpath="last_open_path_readiness.json",
        product_gate_key="overall_open_path_classifier_ready",
        min_new_units_default=5,
        max_staleness_hours_default=6,
        poll_hours_default=6,
        supports_shadow=True,
        config_prefix="ML_OPEN_PATH",
        legacy_env={
            "OPEN_PATH_ML_REFRESH_APPLY_DATA": "1",
            "OPEN_PATH_ML_REFRESH_INCREMENTAL_TRAIN": "1",
            "OPEN_PATH_ML_CONTINUOUS_TRAIN": "1",
        },
    ),
    "earnings_grid": MlContourSpec(
        contour_id="earnings_grid",
        display_name_ru="Earnings grid (scenario + spillover)",
        data_unit="labeled_event",
        refresh_script="scripts/run_earnings_ml_refresh.py",
        train_metrics_relpath="last_event_reaction_scenario_train_metrics.json",
        refresh_log_relpath="last_earnings_ml_refresh.json",
        readiness_relpath="last_earnings_intelligence_readiness.json",
        product_gate_key="overall_earnings_autoprep_ready",
        min_new_units_default=3,
        max_staleness_hours_default=6,
        poll_hours_default=6,
        supports_shadow=True,
        config_prefix="ML_EARNINGS_GRID",
        legacy_env={
            "EARNINGS_ML_REFRESH_APPLY_DATA": "1",
            "EARNINGS_ML_REFRESH_INCREMENTAL_TRAIN": "1",
        },
    ),
    "game5m_entry": MlContourSpec(
        contour_id="game5m_entry",
        display_name_ru="GAME_5M entry CatBoost",
        data_unit="closed_trade",
        refresh_script="scripts/run_game5m_entry_ml_refresh.py",
        train_metrics_relpath="last_game5m_train_metrics.json",
        refresh_log_relpath="last_game5m_entry_ml_refresh.json",
        product_gate_key="overall_production_ready",
        min_new_units_default=8,
        max_staleness_hours_default=168,
        poll_hours_default=24,
        supports_shadow=False,
        config_prefix="ML_GAME5M_ENTRY",
    ),
    "portfolio": MlContourSpec(
        contour_id="portfolio",
        display_name_ru="Portfolio return CatBoost",
        data_unit="closed_trade",
        refresh_script="scripts/run_portfolio_ml_refresh.py",
        train_metrics_relpath="last_portfolio_train_metrics.json",
        refresh_log_relpath="last_portfolio_ml_refresh.json",
        min_new_units_default=5,
        max_staleness_hours_default=168,
        poll_hours_default=24,
        supports_shadow=False,
        config_prefix="ML_PORTFOLIO",
    ),
    "event_reaction_regression": MlContourSpec(
        contour_id="event_reaction_regression",
        display_name_ru="Event-reaction regression (5d log-ret)",
        data_unit="labeled_event",
        refresh_script="scripts/run_event_reaction_ml_refresh.py",
        train_metrics_relpath="last_event_reaction_train_metrics.json",
        refresh_log_relpath="last_event_reaction_ml_refresh.json",
        min_new_units_default=10,
        max_staleness_hours_default=72,
        poll_hours_default=24,
        supports_shadow=False,
        config_prefix="ML_EVENT_REACTION_REGRESSION",
    ),
    "multiday_lr": MlContourSpec(
        contour_id="multiday_lr",
        display_name_ru="Multiday LR ridge (1–3d)",
        data_unit="daily_row",
        refresh_script="scripts/run_multiday_lr_ml_refresh.py",
        train_metrics_relpath="last_multiday_lr_train_metrics.json",
        refresh_log_relpath="last_multiday_lr_ml_refresh.json",
        min_new_units_default=20,
        max_staleness_hours_default=168,
        poll_hours_default=168,
        supports_shadow=False,
        supports_continuous=True,
        config_prefix="ML_MULTIDAY_LR",
    ),
    "recovery": MlContourSpec(
        contour_id="recovery",
        display_name_ru="Recovery CatBoost (time-exit)",
        data_unit="recovery_export_row",
        refresh_script="scripts/run_recovery_ml_refresh.py",
        train_metrics_relpath="last_recovery_train_metrics.json",
        refresh_log_relpath="last_recovery_ml_refresh.json",
        min_new_units_default=20,
        max_staleness_hours_default=168,
        poll_hours_default=168,
        supports_shadow=False,
        config_prefix="ML_RECOVERY",
    ),
    "gap_forecast": MlContourSpec(
        contour_id="gap_forecast",
        display_name_ru="Premarket gap forecast (OLS log)",
        data_unit="forecast_row",
        refresh_script="scripts/run_gap_forecast_refresh.py",
        train_metrics_relpath="last_gap_forecast_metrics.json",
        refresh_log_relpath="last_gap_forecast_ml_refresh.json",
        min_new_units_default=12,
        max_staleness_hours_default=24,
        poll_hours_default=24,
        supports_shadow=False,
        supports_continuous=True,
        config_prefix="ML_GAP_FORECAST",
    ),
}


def get_contour_spec(contour_id: str) -> MlContourSpec:
    key = (contour_id or "").strip().lower()
    if key not in ML_CONTOUR_REGISTRY:
        raise KeyError(f"unknown ml contour: {contour_id}")
    return ML_CONTOUR_REGISTRY[key]


def build_contour_status_row(
    spec: MlContourSpec,
    project_root: Path,
    *,
    product_ready: bool = False,
    dataset_ready: bool = False,
    train_ready: bool = False,
    new_units_apply: Optional[int] = None,
    new_units_train: Optional[int] = None,
) -> Dict[str, Any]:
    log = load_refresh_log(project_root, spec)
    trigger = evaluate_retrain_trigger(
        spec,
        log,
        new_units_since_last_apply=new_units_apply,
        new_units_since_last_train=new_units_train,
        product_ready=product_ready,
        dataset_ready=dataset_ready,
        train_ready=train_ready,
    )
    continuous = contour_continuous_enabled(spec, product_ready=product_ready)
    return {
        "contour_id": spec.contour_id,
        "display_name_ru": spec.display_name_ru,
        "data_unit": spec.data_unit,
        "phase": trigger.phase,
        "product_ready": product_ready,
        "continuous_train_enabled": continuous,
        "trigger": {
            "should_apply_data": trigger.should_apply_data,
            "should_train": trigger.should_train,
            "should_full_shadow": trigger.should_full_shadow,
            "reasons": trigger.reasons,
            "new_units_since_last_apply": trigger.new_units_since_last_apply,
            "new_units_since_last_train": trigger.new_units_since_last_train,
            "staleness_hours_apply": trigger.staleness_hours_apply,
            "staleness_hours_train": trigger.staleness_hours_train,
        },
        "config": {
            "min_new_units": contour_min_new_units(spec),
            "max_staleness_hours": contour_max_staleness_hours(spec),
            "poll_hours": _cfg_int(
                f"{contour_config_prefix(spec)}_RETRAIN_POLL_HOURS",
                spec.poll_hours_default,
            ),
        },
        "paths": {
            "refresh_script": spec.refresh_script,
            "refresh_log": str(default_refresh_log_path(project_root, spec)),
            "train_metrics": str(path_for_spec(project_root, spec, spec.train_metrics_relpath)),
        },
        "last_refresh": log,
    }


def write_aggregate_contours_status(
    project_root: Path,
    rows: List[Dict[str, Any]],
) -> Path:
    path = default_aggregate_status_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "contours": rows,
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def collect_aggregate_contours_status(
    project_root: Path,
    *,
    readiness_resolver: Optional[Callable[[MlContourSpec], Dict[str, bool]]] = None,
    delta_resolver: Optional[Callable[[MlContourSpec], Dict[str, Optional[int]]]] = None,
) -> Dict[str, Any]:
    """
    Build ml_contours_status.json. readiness_resolver / delta_resolver hook DB/file reads per contour.
    """
    rows: List[Dict[str, Any]] = []
    for spec in ML_CONTOUR_REGISTRY.values():
        gates = readiness_resolver(spec) if readiness_resolver else {}
        deltas = delta_resolver(spec) if delta_resolver else {}
        rows.append(
            build_contour_status_row(
                spec,
                project_root,
                product_ready=bool(gates.get("product_ready")),
                dataset_ready=bool(gates.get("dataset_ready")),
                train_ready=bool(gates.get("train_ready")),
                new_units_apply=deltas.get("new_units_apply"),
                new_units_train=deltas.get("new_units_train"),
            )
        )
    write_aggregate_contours_status(project_root, rows)
    return {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "contours": rows}

# -*- coding: utf-8 -*-
"""
Контуры ML: где исполняется сигнал (legacy hot path vs decision_stack shadow).

Принцип dual-track:
- Legacy (`technical_decision_effective`, portfolio guards) исполняет всё с tier promoted|legacy_apply.
- Decision stack (`decision_snapshot`) идёт параллельно; при RESOLVE=false не подменяет legacy.
- RESOLVE=true — опциональный единый исполнитель (session veto и др.).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ContourProductSpec:
    contour_id: str
    display_name: str
    surface: str  # GAME_5M | PORTFOLIO | EARNINGS
    l1_refresh: str
    product_tier: str  # promoted | legacy_apply | advisory | shadow | telemetry | disabled
    legacy_flag: str  # config key that enables legacy execution (or "—")
    legacy_default: str
    stack_contour_id: str
    resolve_required: bool  # True = only via DECISION_STACK_RESOLVE, not legacy
    notes: str


# Источник правды по wiring. Обновлять при promotion/defer.
CONTOUR_PRODUCT_SPECS: tuple[ContourProductSpec, ...] = (
    ContourProductSpec(
        contour_id="portfolio",
        display_name="Portfolio return CatBoost",
        surface="PORTFOLIO",
        l1_refresh="run_ml_refresh_dispatcher",
        product_tier="promoted",
        legacy_flag="PORTFOLIO_CATBOOST_ENABLED",
        legacy_default="false",
        stack_contour_id="portfolio_catboost",
        resolve_required=False,
        notes="L2✅ L3✅: карточки, BLOCK_BUY_ON_WEAK, entry_score — без RESOLVE.",
    ),
    ContourProductSpec(
        contour_id="multiday_lr",
        display_name="Multiday ridge + gates",
        surface="GAME_5M",
        l1_refresh="run_multiday_lr_ml_refresh.py",
        product_tier="legacy_apply",
        legacy_flag="GAME_5M_MULTIDAY_ENTRY_GATE_MODE",
        legacy_default="log_only",
        stack_contour_id="multiday_lr",
        resolve_required=False,
        notes="Entry apply на legacy (finalize_technical_decision_with_multiday); hold log_only.",
    ),
    ContourProductSpec(
        contour_id="game5m_entry",
        display_name="GAME_5M entry CatBoost",
        surface="GAME_5M",
        l1_refresh="run_game5m_entry_ml_refresh.py",
        product_tier="disabled",
        legacy_flag="GAME_5M_CATBOOST_ENABLED",
        legacy_default="false",
        stack_contour_id="catboost_entry_5m",
        resolve_required=False,
        notes="AUC gate + n_valid≥80 before legacy; currently shadow only (see ML_READINESS_GAME5M_*).",
    ),
    ContourProductSpec(
        contour_id="recovery",
        display_name="Recovery TIME_EXIT CatBoost",
        surface="GAME_5M",
        l1_refresh="run_recovery_ml_refresh.py",
        product_tier="telemetry",
        legacy_flag="GAME_5M_RECOVERY_ML_ENABLED",
        legacy_default="false",
        stack_contour_id="recovery_ml",
        resolve_required=False,
        notes="D4a telemetry only; latest AUC ~0.51 — D4b defer until arbiter go.",
    ),
    ContourProductSpec(
        contour_id="gap_forecast",
        display_name="Gap forecast ML vs premarket baseline",
        surface="GAME_5M",
        l1_refresh="run_gap_forecast_refresh.py",
        product_tier="advisory",
        legacy_flag="—",
        legacy_default="—",
        stack_contour_id="gap_forecast",
        resolve_required=True,
        notes="PM baseline primary (policy=auto); ML ridge advisory until rolling 14d+30d beat PM.",
    ),
    ContourProductSpec(
        contour_id="event_reaction_regression",
        display_name="Event reaction 5d regression",
        surface="EARNINGS",
        l1_refresh="run_event_reaction_ml_refresh.py",
        product_tier="advisory",
        legacy_flag="EVENT_REACTION_CATBOOST_ENABLED",
        legacy_default="false",
        stack_contour_id="event_reaction",
        resolve_required=False,
        notes="Advisory в brief/UI; RMSE gate ❌ — не hard-block сделок.",
    ),
    ContourProductSpec(
        contour_id="earnings_grid",
        display_name="Earnings scenario classifier + grid",
        surface="EARNINGS",
        l1_refresh="run_earnings_ml_refresh.py",
        product_tier="shadow",
        legacy_flag="—",
        legacy_default="—",
        stack_contour_id="—",
        resolve_required=False,
        notes="Shadow/UI; не блокирует GAME_5M hot path.",
    ),
    ContourProductSpec(
        contour_id="open_path",
        display_name="Open-path scenario classifier",
        surface="EARNINGS",
        l1_refresh="run_open_path_ml_refresh.py",
        product_tier="shadow",
        legacy_flag="OPEN_PATH_CLASSIFIER_ENABLED",
        legacy_default="false",
        stack_contour_id="—",
        resolve_required=False,
        notes="Prerequisites не готовы; shadow only.",
    ),
)


def _cfg(key: str, default: str = "") -> str:
    try:
        from config_loader import get_config_value

        return (get_config_value(key, default) or default).strip()
    except Exception:
        return default


def _truthy(val: str) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "apply")


def build_contour_runtime_status() -> List[Dict[str, Any]]:
    """Текущий runtime: legacy on/off и роль stack."""
    resolve = _truthy(_cfg("DECISION_STACK_RESOLVE_ENABLED", "false"))
    own_finalize = _truthy(_cfg("DECISION_STACK_OWN_FINALIZE", "true"))
    rows: List[Dict[str, Any]] = []
    for spec in CONTOUR_PRODUCT_SPECS:
        legacy_val = _cfg(spec.legacy_flag, spec.legacy_default) if spec.legacy_flag != "—" else ""
        if spec.legacy_flag == "—":
            legacy_on = spec.product_tier in ("promoted", "legacy_apply", "advisory", "telemetry")
            legacy_detail = "wired in code (no single flag)"
        elif spec.contour_id == "multiday_lr":
            reg_on = _truthy(_cfg("GAME_5M_MULTIDAY_LR_REG_ENABLED", "false"))
            entry_mode = legacy_val or spec.legacy_default
            legacy_on = reg_on and entry_mode in ("apply", "log_only")
            legacy_detail = f"REG={reg_on} entry={entry_mode}"
        else:
            legacy_on = _truthy(legacy_val) if spec.legacy_flag.endswith("_ENABLED") else bool(legacy_val)
            legacy_detail = f"{spec.legacy_flag}={legacy_val or spec.legacy_default}"

        executes_on_legacy = (
            spec.product_tier in ("promoted", "legacy_apply")
            and legacy_on
            and not spec.resolve_required
        )
        rows.append(
            {
                "contour_id": spec.contour_id,
                "display_name": spec.display_name,
                "surface": spec.surface,
                "product_tier": spec.product_tier,
                "l1_refresh": spec.l1_refresh,
                "legacy_executes": executes_on_legacy,
                "legacy_detail": legacy_detail,
                "stack_contour_id": spec.stack_contour_id,
                "stack_role": "executor" if resolve else "shadow",
                "resolve_required_for_ml": spec.resolve_required,
                "own_finalize": own_finalize,
                "notes": spec.notes,
            }
        )
    return rows


def build_dual_track_summary() -> Dict[str, Any]:
    rows = build_contour_runtime_status()
    return {
        "decision_stack_enabled": _truthy(_cfg("DECISION_STACK_ENABLED", "true")),
        "decision_stack_resolve": _truthy(_cfg("DECISION_STACK_RESOLVE_ENABLED", "false")),
        "decision_stack_own_finalize": _truthy(_cfg("DECISION_STACK_OWN_FINALIZE", "true")),
        "executor": "decision_stack_resolve" if _truthy(_cfg("DECISION_STACK_RESOLVE_ENABLED", "false")) else "legacy",
        "legacy_hot_path_field": "technical_decision_effective",
        "stack_shadow_field": "decision_snapshot.projected_effective_if_resolve",
        "promoted_on_legacy": [r["contour_id"] for r in rows if r["legacy_executes"]],
        "contours": rows,
    }

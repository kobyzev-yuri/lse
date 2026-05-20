# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

READINESS_TELEMETRY = "telemetry"
READINESS_CAUTION = "caution"
READINESS_PRODUCTION = "production"

# Порядок veto при DECISION_STACK_RESOLVE_ENABLED (фазы 2–3)
GAME5M_VETO_ORDER = (
    "session",
    "macro_risk",
    "entry_advice",
    "kb_news",
    "news_fusion",
    "gap_forecast",
    "catboost_entry_5m",
    "multiday_lr",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg_bool(key: str, default: bool = False) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _cfg_float(key: str, default: float) -> float:
    from config_loader import get_config_value

    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def gate_mode(key: str, default: str = "log_only") -> str:
    """none | log_only | apply — как у multiday gates."""
    from config_loader import get_config_value

    raw = (get_config_value(key, default) or default).strip().lower()
    if raw in ("none", "log_only", "apply"):
        return raw
    if raw in ("log-only", "logonly"):
        return "log_only"
    return default


def decision_strength_from_signal(signal: Optional[str]) -> float:
    """Грубая шкала для contribution.strength."""
    s = (signal or "HOLD").strip().upper()
    if s == "STRONG_BUY":
        return 0.85
    if s == "BUY":
        return 0.55
    if s == "STRONG_SELL":
        return -0.85
    if s == "SELL":
        return -0.55
    return 0.0


def make_contribution(
    *,
    contour_id: str,
    role: str,
    readiness: str,
    strength: float,
    weight: float,
    action: str,
    detail: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "contour_id": contour_id,
        "role": role,
        "readiness": readiness,
        "strength": round(float(strength), 4),
        "weight": round(float(weight), 4),
        "action": action,
        "detail": detail,
        "metrics": metrics or {},
    }


def default_readiness(contour_id: str) -> str:
    """Дефолтная готовность контура (без чтения анализатора)."""
    prod = {
        "rules_5m",
        "kb_news",
        "entry_advice",
        "macro_risk",
        "strategy_rules",
    }
    caution = {
        "news_fusion",
        "gap_forecast",
        "catboost_entry_5m",
        "multiday_lr",
        "portfolio_catboost",
        "cluster_context",
    }
    if contour_id in prod:
        return READINESS_PRODUCTION
    if contour_id in caution:
        return READINESS_CAUTION
    return READINESS_TELEMETRY


def weight_for_readiness(readiness: str) -> float:
    if readiness == READINESS_PRODUCTION:
        return 1.0
    if readiness == READINESS_CAUTION:
        return 0.35
    return 0.0

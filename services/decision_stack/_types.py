# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
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
    "premarket_gap_baseline",
    "forecast_layer",
    "gap_forecast",
    "catboost_entry_5m",
    "multiday_lr",
    "earnings_trust",
)

PORTFOLIO_VETO_ORDER = (
    "session",
    "cluster_context",
    "portfolio_catboost",
    "event_reaction",
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
        "premarket_gap_baseline",
    }
    caution = {
        "news_fusion",
        "forecast_layer",
        "gap_forecast",
        "catboost_entry_5m",
        "multiday_lr",
        "portfolio_catboost",
        "event_reaction",
        "recovery_ml",
        "cluster_context",
        "earnings_trust",
    }
    if contour_id in prod:
        return READINESS_PRODUCTION
    if contour_id in caution:
        return READINESS_CAUTION
    return READINESS_TELEMETRY


@lru_cache(maxsize=1)
def _latest_ml_train_readiness() -> Dict[str, Any]:
    """Best-effort tail of ml_train_readiness.jsonl; hot-path safe if the file is absent."""
    paths = (
        Path("/app/logs/ml/logs/ml_train_readiness.jsonl"),
        Path(__file__).resolve().parents[2] / "local" / "logs" / "ml_train_readiness.jsonl",
    )
    for p in paths:
        try:
            if not p.is_file():
                continue
            last = ""
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s:
                        last = s
            if last:
                row = json.loads(last)
                if isinstance(row, dict):
                    row["_source_path"] = str(p)
                    return row
        except Exception:
            continue
    return {}


def readiness_from_latest_report(contour_id: str) -> Optional[str]:
    """Map nightly readiness gates to stack readiness without changing config flags."""
    row = _latest_ml_train_readiness()
    if not row:
        return None
    aliases = {
        "portfolio_catboost": ("portfolio",),
        "catboost_entry_5m": ("game5m", "entry_catboost", "catboost_entry"),
        "multiday_lr": ("multiday_lr", "game5m_multiday"),
        "recovery_ml": ("recovery", "game5m_recovery"),
        "event_reaction": ("event_reaction",),
    }
    for key in aliases.get(contour_id, (contour_id,)):
        block = row.get(key)
        if not isinstance(block, dict):
            continue
        gate = block.get("gate") if isinstance(block.get("gate"), dict) else block
        ready = gate.get("ready") if isinstance(gate, dict) else None
        if ready is True:
            return READINESS_PRODUCTION
        if ready is False:
            return READINESS_CAUTION
    return None


def stack_readiness(contour_id: str) -> str:
    """Readiness used by decision stack: nightly gate if present, otherwise static default."""
    try:
        from config_loader import get_config_value

        key = f"DECISION_STACK_READINESS_{contour_id.upper()}".replace("-", "_")
        override = (get_config_value(key, "") or "").strip().lower()
        if override in (READINESS_TELEMETRY, READINESS_CAUTION, READINESS_PRODUCTION):
            return override
    except Exception:
        pass
    return readiness_from_latest_report(contour_id) or default_readiness(contour_id)


def weight_for_readiness(readiness: str) -> float:
    if readiness == READINESS_PRODUCTION:
        return 1.0
    if readiness == READINESS_CAUTION:
        return 0.35
    return 0.0


@lru_cache(maxsize=1)
def _latest_trust_arbiter_weights() -> Dict[str, float]:
    paths = (
        Path("/app/logs/ml/ml_data_quality/last_unified_trust_arbiter.json"),
        Path(__file__).resolve().parents[2] / "local" / "logs" / "ml_data_quality" / "last_unified_trust_arbiter.json",
    )
    for p in paths:
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            weights = data.get("decision_stack_weights")
            if isinstance(weights, dict):
                return {str(k): float(v) for k, v in weights.items()}
        except Exception:
            continue
    return {}


def trust_score_for_contour(contour_id: str) -> float:
    """L2.5 trust multiplier from last unified arbiter (1.0 if artifact missing)."""
    weights = _latest_trust_arbiter_weights()
    if not weights:
        return 1.0
    base = weight_for_readiness(stack_readiness(contour_id))
    if base <= 0:
        return 0.0
    w = weights.get(contour_id)
    if w is None:
        return 1.0
    return min(1.0, max(0.0, float(w) / base))


def effective_stack_weight(contour_id: str, readiness: str) -> float:
    """weight_for_readiness × trust_score (L3 stack)."""
    base = weight_for_readiness(readiness)
    if base <= 0:
        return 0.0
    return round(base * trust_score_for_contour(contour_id), 4)

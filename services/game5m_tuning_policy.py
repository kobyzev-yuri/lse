"""Shared policy for GAME_5M parameter tuning.

This module is intentionally small and conservative: it centralizes which
GAME_5M config keys may be changed by automated/replay tooling and how large a
single step may be. Runtime still reads config.env via config_loader.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from config_loader import get_config_value, is_editable_config_env_key, update_config_key


DEFAULT_DENY_KEYS = frozenset(
    {
        "GAME_5M_SIGNAL_CRON_MINUTES",
    }
)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str
    key: str
    proposed: str
    current: Optional[str] = None


def coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def normalize_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value if value is not None else "").strip()


def current_config_value(key: str) -> Optional[str]:
    val = get_config_value(key, None)
    return None if val is None else str(val)


def _step_limit_reason(key: str, current: Optional[str], proposed: str) -> Optional[str]:
    proposed_f = coerce_float(proposed)
    if proposed_f is None:
        return "non_numeric_value"
    current_f = coerce_float(current)
    if current_f is None:
        return None
    delta = proposed_f - current_f
    if key == "GAME_5M_TAKE_MOMENTUM_FACTOR" and abs(delta) > 0.1:
        return "delta_too_large_take_momentum_factor"
    if key == "GAME_5M_TAKE_PROFIT_PCT" and abs(delta) > 1.0:
        return "delta_too_large_take_profit_pct"
    if key.startswith("GAME_5M_TAKE_PROFIT_PCT_") and abs(delta) > 2.0:
        return "delta_too_large_take_profit_pct_ticker"
    if key == "GAME_5M_TAKE_PROFIT_MIN_PCT" and abs(delta) > 1.0:
        return "delta_too_large_take_profit_min_pct"
    if key in ("GAME_5M_RSI_STRONG_BUY_MAX", "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX") and abs(delta) > 5:
        return "delta_too_large_rsi"
    if key in ("GAME_5M_VOLATILITY_WARN_BUY_MIN", "GAME_5M_VOLATILITY_WAIT_MIN") and abs(delta) > 0.3:
        return "delta_too_large_volatility"
    if key in ("GAME_5M_MAX_POSITION_DAYS",) and abs(delta) > 1:
        return "delta_too_large_max_position_days"
    if key in ("GAME_5M_MAX_POSITION_MINUTES",) and abs(delta) > 240:
        return "delta_too_large_max_position_minutes"
    if key.startswith("GAME_5M_MAX_POSITION_DAYS_") and abs(delta) > 1:
        return "delta_too_large_max_position_days_ticker"
    return None


def validate_game5m_update(
    key: str,
    proposed: Any,
    *,
    current: Optional[Any] = None,
    deny_keys: Iterable[str] = DEFAULT_DENY_KEYS,
    enforce_step_limits: bool = True,
) -> ValidationResult:
    key = str(key or "").strip()
    proposed_s = normalize_value(proposed)
    current_s = normalize_value(current) if current is not None else current_config_value(key)
    deny = {str(k).strip().upper() for k in deny_keys if str(k).strip()}

    if not key or not proposed_s:
        return ValidationResult(False, "empty_key_or_value", key, proposed_s, current_s)
    if key.upper() in deny:
        return ValidationResult(False, "deny_list", key, proposed_s, current_s)
    if not key.startswith("GAME_5M_"):
        return ValidationResult(False, "non_game5m_key", key, proposed_s, current_s)
    if not is_editable_config_env_key(key):
        return ValidationResult(False, "not_editable", key, proposed_s, current_s)

    if proposed_s.lower() in ("true", "false", "1", "0", "yes", "no"):
        return ValidationResult(True, "ok", key, proposed_s, current_s)

    proposed_f = coerce_float(proposed_s)
    if proposed_f is None:
        return ValidationResult(False, "non_numeric_value", key, proposed_s, current_s)
    if proposed_f < 0 and not key.endswith("_MAX_LOSS_PCT"):
        return ValidationResult(False, "negative_value_not_allowed", key, proposed_s, current_s)

    if enforce_step_limits:
        reason = _step_limit_reason(key, current_s, proposed_s)
        if reason:
            return ValidationResult(False, reason, key, proposed_s, current_s)
    return ValidationResult(True, "ok", key, proposed_s, current_s)


def apply_game5m_update(
    key: str,
    proposed: Any,
    *,
    source: str = "unknown",
    dry_run: bool = False,
    deny_keys: Iterable[str] = DEFAULT_DENY_KEYS,
    enforce_step_limits: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    validation = validate_game5m_update(
        key,
        proposed,
        deny_keys=deny_keys,
        enforce_step_limits=enforce_step_limits,
    )
    record: Dict[str, Any] = {
        "env_key": validation.key,
        "old_value": validation.current,
        "new_value": validation.proposed,
        "source": source,
        "dry_run": bool(dry_run),
        "validation": {"ok": validation.ok, "reason": validation.reason},
        "status": "not_applied",
    }
    if not validation.ok:
        record["status"] = "rejected"
        return False, record
    if dry_run:
        record["status"] = "dry_run"
        return True, record
    ok = update_config_key(validation.key, validation.proposed)
    record["status"] = "applied" if ok else "write_failed"
    return ok, record

"""Shared policy for GAME_5M and PORTFOLIO parameter tuning via config.env.

Centralizes which keys may be changed by analyzer / replay tooling and step limits.
Runtime still reads config.env via config_loader.
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

PORTFOLIO_DENY_KEYS = frozenset(
    {
        "PORTFOLIO_CATBOOST_MODEL_PATH",
        "PORTFOLIO_ML_REPORT_JSONL",
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


def _portfolio_step_limit_reason(key: str, current: Optional[str], proposed: str) -> Optional[str]:
    proposed_f = coerce_float(proposed)
    if proposed_f is None:
        return "non_numeric_value"
    current_f = coerce_float(current)
    if current_f is None:
        return None
    delta = proposed_f - current_f
    if key in ("PORTFOLIO_TRAILING_PULLBACK_PCT", "PORTFOLIO_TRAILING_MIN_PROFIT_PCT") and abs(delta) > 3.0:
        return "delta_too_large_trailing"
    if key in ("PORTFOLIO_ML_TAKE_CAP_PCT", "PORTFOLIO_ML_TAKE_FLOOR_PCT") and abs(delta) > 8.0:
        return "delta_too_large_ml_take_pct"
    if key == "PORTFOLIO_ML_TAKE_FACTOR" and abs(delta) > 0.5:
        return "delta_too_large_ml_take_factor"
    if key == "PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE" and abs(delta) > 12:
        return "delta_too_large_hold_below_score"
    if key.endswith("_TAKE_PROFIT_PCT") or key == "PORTFOLIO_TAKE_PROFIT_PCT":
        if abs(delta) > 4.0:
            return "delta_too_large_take_profit_pct"
    if key.endswith("_STOP_LOSS_PCT"):
        if abs(delta) > 3.0:
            return "delta_too_large_stop_loss_pct"
    return None


def validate_portfolio_update(
    key: str,
    proposed: Any,
    *,
    current: Optional[Any] = None,
    deny_keys: Iterable[str] = PORTFOLIO_DENY_KEYS,
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
    if not key.startswith("PORTFOLIO_"):
        return ValidationResult(False, "non_portfolio_key", key, proposed_s, current_s)
    if not is_editable_config_env_key(key):
        return ValidationResult(False, "not_editable", key, proposed_s, current_s)

    if proposed_s.lower() in ("true", "false", "1", "0", "yes", "no"):
        return ValidationResult(True, "ok", key, proposed_s, current_s)

    proposed_f = coerce_float(proposed_s)
    if proposed_f is None:
        return ValidationResult(False, "non_numeric_value", key, proposed_s, current_s)
    if proposed_f < 0 and "_SCORE" not in key:
        return ValidationResult(False, "negative_value_not_allowed", key, proposed_s, current_s)

    if enforce_step_limits:
        reason = _portfolio_step_limit_reason(key, current_s, proposed_s)
        if reason:
            return ValidationResult(False, reason, key, proposed_s, current_s)
    return ValidationResult(True, "ok", key, proposed_s, current_s)


def validate_config_env_update(
    key: str,
    proposed: Any,
    **kwargs: Any,
) -> ValidationResult:
    """GAME_5M_* или PORTFOLIO_* — для /api/analyzer/apply-config."""
    k = str(key or "").strip()
    if k.startswith("GAME_5M_"):
        return validate_game5m_update(k, proposed, **kwargs)
    if k.startswith("PORTFOLIO_"):
        return validate_portfolio_update(k, proposed, **kwargs)
    return ValidationResult(False, "unknown_key_prefix", k, normalize_value(proposed), current_config_value(k))


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

    if key == "GAME_5M_HANGER_TUNE_JSON":
        if len(proposed_s) > 512:
            return ValidationResult(False, "path_too_long", key, proposed_s, current_s)
        return ValidationResult(True, "ok", key, proposed_s, current_s)

    if proposed_s.lower() in ("true", "false", "1", "0", "yes", "no"):
        return ValidationResult(True, "ok", key, proposed_s, current_s)

    if key.endswith("_GATE_MODE") and proposed_s.lower() in ("none", "log_only", "apply"):
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


def apply_config_env_update(
    key: str,
    proposed: Any,
    *,
    source: str = "unknown",
    dry_run: bool = False,
    enforce_step_limits: bool = True,
) -> Tuple[bool, Dict[str, Any]]:
    """Применить одно изменение config.env (GAME_5M или PORTFOLIO)."""
    k = str(key or "").strip()
    deny_keys: Iterable[str] = DEFAULT_DENY_KEYS if k.startswith("GAME_5M_") else PORTFOLIO_DENY_KEYS
    validation = validate_config_env_update(
        k,
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


def validate_game5m_bundle(
    bundle_id: str,
    *,
    enforce_step_limits: bool = False,
) -> Tuple[bool, str, list]:
    from services.game5m_tuning_bundles import get_bundle

    try:
        bundle = get_bundle(bundle_id)
    except KeyError as e:
        return False, str(e), []
    results: list = []
    for key, proposed in bundle.changes.items():
        vr = validate_game5m_update(key, proposed, enforce_step_limits=enforce_step_limits)
        results.append(vr)
        if not vr.ok:
            return False, vr.reason, results
    return True, "ok", results


def apply_game5m_bundle(
    bundle_id: str,
    *,
    source: str = "unknown",
    dry_run: bool = False,
    enforce_step_limits: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """Apply coordinated multi-key bundle; all keys validated before any write."""
    from services.game5m_tuning_bundles import get_bundle

    bundle = get_bundle(bundle_id)
    ok_all, reason, _validations = validate_game5m_bundle(
        bundle_id,
        enforce_step_limits=enforce_step_limits,
    )
    payload: Dict[str, Any] = {
        "bundle_id": bundle.bundle_id,
        "description_ru": bundle.description_ru,
        "dry_run": bool(dry_run),
        "validation_ok": ok_all,
        "validation_reason": reason,
        "records": [],
        "status": "rejected" if not ok_all else "pending",
    }
    if not ok_all:
        return False, payload

    records: list = []
    for key, proposed in bundle.changes.items():
        rec_ok, record = apply_game5m_update(
            key,
            proposed,
            source=source,
            dry_run=dry_run,
            enforce_step_limits=enforce_step_limits,
        )
        records.append(record)
        if not rec_ok:
            payload["records"] = records
            payload["status"] = "partial_failed"
            return False, payload

    payload["records"] = records
    payload["status"] = "dry_run" if dry_run else "applied"
    return True, payload


def rollback_game5m_bundle_applied(
    applied_payload: Dict[str, Any],
    *,
    source: str,
) -> Tuple[bool, list]:
    """Restore old values from bundle apply records (reverse order)."""
    records = applied_payload.get("records") if isinstance(applied_payload.get("records"), list) else []
    out: list = []
    ok_all = True
    for rec in reversed(records):
        if not isinstance(rec, dict):
            continue
        key = str(rec.get("env_key") or "").strip()
        old = rec.get("old_value")
        if not key or old is None:
            continue
        ok = update_config_key(key, normalize_value(old))
        out.append(
            {
                "env_key": key,
                "old_value": rec.get("new_value"),
                "new_value": old,
                "source": source,
                "status": "rolled_back" if ok else "write_failed",
            }
        )
        ok_all = ok_all and ok
    return ok_all, out


def rollback_game5m_experiment_applied(
    applied: Dict[str, Any],
    *,
    source: str,
) -> Tuple[bool, Any]:
    """Rollback single-key or bundle experiment from ledger `applied` payload."""
    if isinstance(applied.get("records"), list) and applied.get("bundle_id"):
        return rollback_game5m_bundle_applied(applied, source=source)
    key = str(applied.get("env_key") or "").strip()
    old_value = applied.get("old_value")
    if not key or old_value is None:
        return False, {"status": "missing_rollback_target"}
    ok, record = apply_game5m_update(key, old_value, source=source, enforce_step_limits=False)
    return ok, record

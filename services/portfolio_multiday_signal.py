"""
Портфель: multiday ridge (дневки + premarket + news + calendar) для входа.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def portfolio_multiday_enabled() -> bool:
    if _truthy(get_config_value("PORTFOLIO_MULTIDAY_LR_ENABLED", "")):
        return True
    return _truthy(get_config_value("GAME_5M_MULTIDAY_LR_REG_ENABLED", "false"))


def portfolio_multiday_snapshot(ticker: str) -> Dict[str, Any]:
    """Плоские поля multiday_lr_* для context_json / decision_stack."""
    out: Dict[str, Any] = {"portfolio_multiday_status": "disabled"}
    if not portfolio_multiday_enabled():
        return out
    try:
        from report_generator import get_engine
        from services.log_return_multiday_forecast import (
            compute_log_return_multiday_forecast,
            format_multiday_forecast_one_line,
        )

        eng = get_engine()
        fc = compute_log_return_multiday_forecast(
            ticker,
            db_engine=eng,
            use_intraday_features=False,
        )
        if not fc or fc.get("unavailable"):
            out["portfolio_multiday_status"] = "unavailable"
            out["portfolio_multiday_note"] = fc.get("error") if isinstance(fc, dict) else "no_forecast"
            out["multiday_lr_forecast_unavailable"] = True
            return out

        out["portfolio_multiday_status"] = "ok"
        out["log_return_multiday_forecast"] = fc
        out["log_return_multiday_forecast_summary"] = format_multiday_forecast_one_line(fc)
        hz = fc.get("horizons") if isinstance(fc.get("horizons"), dict) else {}
        for h in (1, 2, 3):
            cell = hz.get(str(h))
            if isinstance(cell, dict) and cell.get("predicted_pct_vs_spot") is not None:
                out[f"multiday_lr_horizon_{h}d_pct_vs_spot"] = round(float(cell["predicted_pct_vs_spot"]), 3)
        bs = fc.get("bias_summary")
        if bs:
            out["multiday_lr_bias"] = bs
        out["multiday_lr_premarket_db_used"] = bool(fc.get("premarket_db_used"))
        out["multiday_lr_news_db_used"] = bool(fc.get("news_db_used"))
        out["multiday_lr_macro_calendar_db_used"] = bool(fc.get("macro_calendar_db_used"))
        out["multiday_lr_symbol_calendar_db_used"] = bool(fc.get("symbol_calendar_db_used"))
        return out
    except Exception as e:
        logger.debug("portfolio_multiday_snapshot %s: %s", ticker, e)
        out["portfolio_multiday_status"] = "error"
        out["portfolio_multiday_note"] = str(e)
        out["multiday_lr_forecast_error"] = str(e)
        return out


def portfolio_multiday_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """True — не открывать portfolio BUY по медвежьему multiday ridge."""
    from services.multiday_lr_gate import evaluate_multiday_entry_gate

    snap = portfolio_multiday_snapshot(ticker)
    if snap.get("portfolio_multiday_status") != "ok":
        return False, ""
    gate = evaluate_multiday_entry_gate(
        snap,
        mode_env_key="PORTFOLIO_MULTIDAY_ENTRY_GATE_MODE",
        tau_1d_env_key="PORTFOLIO_MULTIDAY_ENTRY_TAU_1D_PCT",
        tau_other_env_key="PORTFOLIO_MULTIDAY_ENTRY_TAU_PCT",
        neg_min_env_key="PORTFOLIO_MULTIDAY_ENTRY_NEGATIVE_HORIZONS_MIN",
    )
    mode = (gate.get("mode") or "none").strip().lower()
    if mode != "apply":
        return False, ""
    if gate.get("status") == "ok" and gate.get("would_hold"):
        return True, f"multiday ridge: {gate.get('note') or 'bearish horizons'}"
    return False, ""

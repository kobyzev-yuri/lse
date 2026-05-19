"""
Портфельная игра: CatBoost-фильтр входа и снимок ML для context_json на BUY.
Выход по тейку — ExecutionAgent.check_stop_losses (стратегия / PORTFOLIO_TAKE_PROFIT_PCT).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def portfolio_ml_snapshot(ticker: str) -> Dict[str, Any]:
    """Поля CatBoost для context_json (без исключений наружу)."""
    try:
        from services.portfolio_catboost_signal import predict_portfolio_expected_return

        return dict(predict_portfolio_expected_return(ticker) or {})
    except Exception as e:
        logger.debug("portfolio_ml_snapshot %s: %s", ticker, e)
        return {"portfolio_ml_status": "error", "portfolio_ml_note": str(e)}


def portfolio_catboost_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """
    True — не открывать новый portfolio BUY (докуп при открытой позиции не вызывается).
    """
    if not _truthy(get_config_value("PORTFOLIO_CATBOOST_BLOCK_BUY_ON_WEAK", "true")):
        return False, ""
    try:
        min_score = float((get_config_value("PORTFOLIO_CATBOOST_HOLD_BELOW_SCORE", "48") or "48").strip())
    except (ValueError, TypeError):
        min_score = 48.0

    snap = portfolio_ml_snapshot(ticker)
    status = (snap.get("portfolio_ml_status") or "").strip()
    if status != "ok":
        return False, ""
    score = snap.get("portfolio_ml_entry_score")
    if score is None:
        return False, ""
    try:
        sc = float(score)
    except (TypeError, ValueError):
        return False, ""
    if sc < min_score:
        exp = snap.get("portfolio_ml_expected_return_pct")
        return True, (
            f"CatBoost entry_score={sc:.1f} < {min_score:.1f} "
            f"(expected_5d_pct={exp}, PORTFOLIO_CATBOOST_BLOCK_BUY_ON_WEAK)"
        )
    return False, ""


def merge_portfolio_buy_context(
    context_json: Optional[Dict[str, Any]],
    ticker: str,
    *,
    base_take_profit: Optional[float] = None,
) -> Dict[str, Any]:
    base = dict(context_json) if isinstance(context_json, dict) else {}
    ml = portfolio_ml_snapshot(ticker)
    for k, v in ml.items():
        if k.startswith("portfolio_ml_"):
            base[k] = v
    if base_take_profit is not None and base_take_profit > 0:
        try:
            from services.portfolio_exit_policy import compute_entry_effective_take_for_ticker

            eff, note = compute_entry_effective_take_for_ticker(ticker, float(base_take_profit), base)
            if eff is not None and eff > 0:
                base["portfolio_effective_take_pct_at_entry"] = round(eff, 3)
                base["portfolio_effective_take_note"] = note
        except Exception as e:
            logger.debug("entry effective take %s: %s", ticker, e)
    return base

"""
Portfolio BUY: optional event-reaction CatBoost filter (earnings window).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def event_reaction_ml_snapshot(ticker: str) -> Dict[str, Any]:
    try:
        from services.event_reaction_catboost_signal import predict_event_reaction_for_ticker

        return dict(predict_event_reaction_for_ticker(ticker) or {})
    except Exception as e:
        logger.debug("event_reaction_ml_snapshot %s: %s", ticker, e)
        return {"event_reaction_ml_status": "error", "event_reaction_ml_note": str(e)}


def event_reaction_blocks_buy(ticker: str) -> Tuple[bool, str]:
    """
    True — не открывать portfolio BUY при слабом прогнозе event-reaction в окне earnings.
    """
    if not _truthy(get_config_value("EVENT_REACTION_CATBOOST_ENABLED", "false")):
        return False, ""
    if not _truthy(get_config_value("EVENT_REACTION_BLOCK_BUY_ON_WEAK", "false")):
        return False, ""
    try:
        from services.decision_stack._types import READINESS_PRODUCTION, stack_readiness

        readiness = stack_readiness("event_reaction")
        if readiness != READINESS_PRODUCTION:
            return False, f"Event-reaction readiness={readiness}: runtime block disabled until production gate"
    except Exception:
        pass
    try:
        min_score = float((get_config_value("EVENT_REACTION_HOLD_BELOW_SCORE", "48") or "48").strip())
    except (ValueError, TypeError):
        min_score = 48.0

    snap = event_reaction_ml_snapshot(ticker)
    status = (snap.get("event_reaction_ml_status") or "").strip()
    if status != "ok":
        return False, ""
    score = snap.get("event_reaction_ml_entry_score")
    if score is None:
        return False, ""
    try:
        sc = float(score)
    except (TypeError, ValueError):
        return False, ""
    if sc < min_score:
        exp = snap.get("event_reaction_ml_expected_return_5d_pct")
        evt = snap.get("event_reaction_ml_event_time_et") or "?"
        return True, (
            f"Event-reaction entry_score={sc:.1f} < {min_score:.1f} "
            f"(expected_5d_pct={exp}, event={evt}, EVENT_REACTION_BLOCK_BUY_ON_WEAK)"
        )
    return False, ""

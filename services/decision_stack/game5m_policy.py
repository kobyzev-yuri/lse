# -*- coding: utf-8 -*-
"""Применение ML-гейтов (CatBoost, multiday) — фаза 3, единая точка до snapshot."""

from __future__ import annotations

import logging
from typing import Any, Dict

from services.decision_stack._types import _cfg_bool

logger = logging.getLogger(__name__)


def apply_game5m_policy_gates(d5: Dict[str, Any], ticker: str) -> None:
    """
    CatBoost fusion + multiday entry gate (те же finalize, что в get_decision_5m).
    Вызывать после rules/KB/macro и до decision_snapshot.
    """
    t = (ticker or "").strip().upper()
    d5.setdefault("technical_decision_core", d5.get("decision"))
    d5.setdefault("technical_decision_effective", d5.get("decision"))

    try:
        from config_loader import get_config_value

        cb_on = (get_config_value("GAME_5M_CATBOOST_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    except Exception:
        cb_on = False

    if cb_on:
        try:
            from services.catboost_5m_signal import attach_catboost_signal, finalize_technical_decision_with_catboost

            attach_catboost_signal(d5, t)
            finalize_technical_decision_with_catboost(d5)
        except Exception as e:
            logger.warning("decision_stack catboost %s: %s", t, e)
            d5.setdefault("technical_decision_core", d5.get("decision"))
            d5.setdefault("technical_decision_effective", d5.get("decision"))
            d5.setdefault("catboost_fusion_mode", "none")
    else:
        d5.setdefault("technical_decision_core", d5.get("decision"))
        d5.setdefault("technical_decision_effective", d5.get("decision"))
        d5.setdefault("catboost_fusion_mode", "none")

    try:
        from services.multiday_lr_gate import finalize_technical_decision_with_multiday

        finalize_technical_decision_with_multiday(d5)
    except Exception as e:
        logger.warning("decision_stack multiday %s: %s", t, e)

    try:
        from services.catboost_5m_signal import attach_catboost_bar_v2_signal

        attach_catboost_bar_v2_signal(d5, t)
    except Exception as e:
        logger.warning("decision_stack catboost bar v2 %s: %s", t, e)


def stack_own_finalize_enabled() -> bool:
    """Фаза 3: ML-гейты только внутри decision_stack (по умолчанию true)."""
    return _cfg_bool("DECISION_STACK_OWN_FINALIZE", True)

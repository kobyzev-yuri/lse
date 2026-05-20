# -*- coding: utf-8 -*-
"""Контракт блоков анализатора (фаза 0 rollout)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_analyzer_block(
    *,
    contour_id: str,
    role: str,
    mode: str = "ok",
    phase: str = "C",
    overall_verdict: Optional[str] = None,
    metrics: Optional[Dict[str, Any]] = None,
    thresholds: Optional[Dict[str, Any]] = None,
    rationale_ru: str = "",
    next_steps_ru: Optional[List[str]] = None,
    promotion: Optional[Dict[str, Any]] = None,
    conclusion_ru: str = "",
) -> Dict[str, Any]:
    """Единый формат AnalyzerBlock для payload.contours."""
    return {
        "contour_id": contour_id,
        "role": role,
        "mode": mode,
        "phase": phase,
        "overall_verdict": overall_verdict,
        "metrics": metrics or {},
        "thresholds": thresholds or {},
        "rationale_ru": rationale_ru,
        "next_steps_ru": list(next_steps_ru or []),
        "promotion": promotion,
        "conclusion_ru": conclusion_ru or rationale_ru,
    }


AnalyzerBlock = Dict[str, Any]

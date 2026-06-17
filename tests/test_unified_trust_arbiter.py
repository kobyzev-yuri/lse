"""Tests for unified trust arbiter."""
from __future__ import annotations

from services.unified_trust_arbiter import (
    build_unified_trust_arbiter,
    format_operator_digest_ru,
    recommended_gate_mode,
    trust_label_from_score,
)


def test_trust_label_thresholds():
    assert trust_label_from_score(0.80) == "high"
    assert trust_label_from_score(0.60) == "medium"
    assert trust_label_from_score(0.30) == "low"
    assert trust_label_from_score(0.10) == "insufficient"


def test_recommended_gate_mode_respects_l2():
    assert recommended_gate_mode("high", l2_ready=True) == "apply"
    assert recommended_gate_mode("high", l2_ready=False) == "log_only"
    assert recommended_gate_mode("medium", l2_ready=True) == "caution"
    assert recommended_gate_mode("insufficient", l2_ready=True) == "none"


def test_build_unified_trust_arbiter_minimal():
    arb = build_unified_trust_arbiter(project_root=None, report={})
    assert arb["arbiter_version"] == "trust_v1"
    assert "GAME_5M" in arb["surfaces"]
    assert "EARNINGS" in arb["surfaces"]
    assert arb["operator_digest_ru"].startswith("LSE Trust")
    assert isinstance(arb["decision_stack_weights"], dict)


def test_format_operator_digest_includes_surfaces():
    arb = build_unified_trust_arbiter(project_root=None, report={})
    text = format_operator_digest_ru(arb)
    assert "GAME_5M" in text
    assert "EARNINGS" in text
    assert "Итог:" in text

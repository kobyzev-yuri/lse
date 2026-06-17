"""Tests for unified trust arbiter."""
from __future__ import annotations

from services.unified_trust_arbiter import (
    build_unified_trust_arbiter,
    format_operator_digest_ru,
    multiday_lr_reality_from_wf_artifact,
    recommended_gate_mode,
    resolve_multiday_lr_reality_check,
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


def test_multiday_lr_reality_from_wf_artifact():
    wf = {
        "generated_at_utc": "2026-06-16T00:00:00Z",
        "verdict": "caution",
        "rationale_ru": "test",
        "live_feature_set": "v3nm",
        "v3nm_pooled": {
            "1": {"n_points_sum": 220, "mean_sign_accuracy": 0.54, "mean_rmse_oos_log_across_tickers": 0.09},
        },
    }
    mlr = multiday_lr_reality_from_wf_artifact(wf)
    assert mlr["mode"] == "ok"
    assert mlr["walkforward_production_verdict"] == "caution"
    assert mlr["pooled_by_horizon"]["1"]["n_points_sum"] == 220


def test_resolve_multiday_prefers_report_then_wf():
    report = {"multiday_lr_reality_check": {"mode": "ok", "walkforward_production_verdict": "ready"}}
    mlr = resolve_multiday_lr_reality_check(report, project_root=None)
    assert mlr["walkforward_production_verdict"] == "ready"
    mlr2 = resolve_multiday_lr_reality_check(
        {},
        project_root=None,
    )
    assert isinstance(mlr2, dict)


def test_build_unified_trust_arbiter_minimal():
    arb = build_unified_trust_arbiter(project_root=None, report={})
    assert arb["arbiter_version"] == "trust_v1"
    assert "GAME_5M" in arb["surfaces"]
    assert "EARNINGS" in arb["surfaces"]
    earn = arb["surfaces"]["EARNINGS"]
    assert "context_slices" in earn
    assert arb["operator_digest_ru"].startswith("LSE Trust")
    assert isinstance(arb["decision_stack_weights"], dict)


def test_format_operator_digest_includes_surfaces():
    arb = build_unified_trust_arbiter(project_root=None, report={})
    text = format_operator_digest_ru(arb)
    assert "GAME_5M" in text
    assert "EARNINGS" in text
    assert "Итог:" in text

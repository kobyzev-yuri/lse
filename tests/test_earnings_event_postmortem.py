"""Tests for earnings post-mortem aggregation."""
from __future__ import annotations

from services.earnings_event_postmortem import (
    _fact_was_bad,
    aggregate_earnings_trust_metrics,
)


def test_aggregate_earnings_trust_metrics_sign_accuracy():
    rows = [
        {
            "event_date": "2026-06-10",
            "context": {"scenario_class": "gap_up", "alignment": "conflict"},
            "models": {
                "scenario_sign": {"hit": True, "predicted_scenario": "gap_up"},
                "regression_5d": {"sign_hit": False},
                "peer_spillover": [
                    {"sign_hit": True},
                    {"sign_hit": False},
                ],
            },
            "fusion": {"would_have_blocked": True, "alignment": "conflict"},
            "fusion_outcome": {"fact_was_bad": True, "block_was_correct": True},
        },
        {
            "event_date": "2026-06-05",
            "context": {"scenario_class": "gap_up", "alignment": "aligned_or_weak"},
            "models": {
                "scenario_sign": {"hit": True, "predicted_scenario": "gap_up"},
                "regression_5d": {"sign_hit": True},
                "peer_spillover": [{"sign_hit": True}],
            },
            "fusion": {"would_have_blocked": False, "alignment": "aligned_or_weak"},
            "fusion_outcome": {"fact_was_bad": False, "block_was_correct": True},
        },
    ]
    agg = aggregate_earnings_trust_metrics(rows, window_days=365, context_bucket_min=2)
    scen = agg["contours"]["earnings_scenario"]
    assert scen["n_matured"] == 2
    assert scen["sign_accuracy"] == 1.0
    reg = agg["contours"]["event_reaction"]
    assert reg["sign_accuracy"] == 0.5
    peer = agg["contours"]["peer_spillover"]
    assert peer["n_matured"] == 3
    assert peer["sign_accuracy"] == round(2 / 3, 4)
    assert agg["fusion_blocked_rate"] == 0.5
    fq = agg["fusion_quality"]
    assert fq["block_precision"] == 1.0
    assert "gap_up" in agg["by_scenario_class"]
    assert agg["by_scenario_class"]["gap_up"]["n"] == 2


def test_fact_was_bad_large_miss():
    assert _fact_was_bad(
        fact_5d=-0.08,
        sign_hit=False,
        rmse_bucket="miss_large",
        threshold_log=0.004,
    )

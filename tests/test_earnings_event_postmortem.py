"""Tests for earnings post-mortem aggregation."""
from __future__ import annotations

from services.earnings_event_postmortem import aggregate_earnings_trust_metrics


def test_aggregate_earnings_trust_metrics_sign_accuracy():
    rows = [
        {
            "event_date": "2026-06-10",
            "models": {
                "scenario_sign": {"hit": True},
                "regression_5d": {"sign_hit": False},
                "peer_spillover": [
                    {"sign_hit": True},
                    {"sign_hit": False},
                ],
            },
            "fusion": {"would_have_blocked": True},
        },
        {
            "event_date": "2026-06-05",
            "models": {
                "scenario_sign": {"hit": True},
                "regression_5d": {"sign_hit": True},
                "peer_spillover": [{"sign_hit": True}],
            },
            "fusion": {"would_have_blocked": False},
        },
    ]
    agg = aggregate_earnings_trust_metrics(rows, window_days=365)
    scen = agg["contours"]["earnings_scenario"]
    assert scen["n_matured"] == 2
    assert scen["sign_accuracy"] == 1.0
    reg = agg["contours"]["event_reaction"]
    assert reg["sign_accuracy"] == 0.5
    peer = agg["contours"]["peer_spillover"]
    assert peer["n_matured"] == 3
    assert peer["sign_accuracy"] == round(2 / 3, 4)
    assert agg["fusion_blocked_rate"] == 0.5

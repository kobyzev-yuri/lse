"""Tests for earnings post-mortem aggregation."""
from __future__ import annotations

from datetime import date, timedelta

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
    assert agg["degradation"]["hit_90d"] == 1.0


def test_aggregate_degradation_detects_drop():
    old = date.today() - timedelta(days=60)
    recent = date.today() - timedelta(days=5)
    rows = []
    for i in range(8):
        rows.append(
            {
                "event_date": old.isoformat(),
                "context": {"scenario_class": "gap_up"},
                "models": {"scenario_sign": {"hit": True}},
                "fusion": {},
                "fusion_outcome": {},
            }
        )
    for i in range(4):
        rows.append(
            {
                "event_date": recent.isoformat(),
                "context": {"scenario_class": "gap_up"},
                "models": {"scenario_sign": {"hit": False}},
                "fusion": {},
                "fusion_outcome": {},
            }
        )
    agg = aggregate_earnings_trust_metrics(rows, window_days=365, context_bucket_min=2)
    assert agg["degradation"]["hit_14d"] == 0.0
    assert agg["degradation"]["hit_90d"] is not None
    assert agg["degradation"]["hit_90d"] > agg["degradation"]["hit_14d"]
    assert agg["degradation"]["degrading"] is True


def test_fact_was_bad_large_miss():
    assert _fact_was_bad(
        fact_5d=-0.08,
        sign_hit=False,
        rmse_bucket="miss_large",
        threshold_log=0.004,
    )


def test_format_postmortem_table_rows_verdicts():
    from services.earnings_event_postmortem import format_postmortem_table_rows

    row = {
        "symbol": "ORCL",
        "models": {
            "regression_5d": {"pred": 0.01, "fact": -0.05, "sign_hit": False, "rmse_bucket": "miss_large"},
            "scenario_sign": {
                "predicted_scenario": "gap_up",
                "pred_sign": 1,
                "fact": -0.05,
                "hit": False,
                "class_hit": True,
            },
            "peer_spillover": [{"peer": "NVDA", "pred": 0.02, "fact_5d": 0.01, "sign_hit": True}],
        },
        "fusion": {"would_have_blocked": True, "alignment": "conflict", "conviction": "low"},
        "fusion_outcome": {"fact_was_bad": True, "block_was_correct": True},
    }
    lines = format_postmortem_table_rows(row)
    assert any(l["model"] == "Regression 5d" for l in lines)
    assert any("знак ✗" in l["verdict_ru"] for l in lines)
    assert any(l["tickers"] == "NVDA" for l in lines)

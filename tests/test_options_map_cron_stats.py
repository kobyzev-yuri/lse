# -*- coding: utf-8 -*-
from __future__ import annotations

from services.options_map_cron_stats import (
    _quantiles,
    compute_snapshot_flow_metrics,
    suggest_pcr_thresholds_from_quantiles,
)


def test_quantiles_basic():
    q = _quantiles([0.6, 0.8, 1.0, 1.2, 1.4], (0.25, 0.5, 0.75))
    assert q["p25"] == 0.8
    assert q["p50"] == 1.0
    assert q["p75"] == 1.2


def test_compute_snapshot_flow_metrics():
    contracts = [
        {"contract_type": "put", "strike": 1000.0, "open_interest": 5000, "volume": 200},
        {"contract_type": "call", "strike": 1100.0, "open_interest": 3000, "volume": 400},
        {"contract_type": "put", "strike": 2000.0, "open_interest": 9000, "volume": 50},
    ]
    m = compute_snapshot_flow_metrics(contracts, spot=1050.0, strike_window_pct=0.20)
    assert m["status"] == "ok"
    assert m["pcr_volume"] == 0.5
    assert m["put_volume"] == 200
    assert m["call_volume"] == 400


def test_suggest_thresholds_fallback_when_few_samples():
    s = suggest_pcr_thresholds_from_quantiles([0.7, 0.9, 1.1], min_samples=10)
    assert s["ready"] is False
    assert s["source"] == "wireframe_fallback"


def test_lookup_cron_pcr_recommendation_exact():
    from services.options_map_cron_stats import lookup_cron_pcr_recommendation

    artifact = {
        "status": "ok",
        "generated_at_utc": "2026-06-16T12:00:00Z",
        "days": 90,
        "strike_window_pct": 0.20,
        "min_samples_for_quantile_thresholds": 10,
        "method_ru": "test",
        "ticker_exp_series": [
            {
                "ticker": "MU",
                "expiration_date": "2026-06-26",
                "snapshot_count": 12,
                "pcr_volume_stats": {"p25": 0.72, "p50": 0.95, "p75": 1.18},
                "suggested_thresholds": {
                    "ready": True,
                    "pcr_volume_bullish_max": 0.72,
                    "pcr_volume_bearish_min": 1.18,
                    "reason_ru": "ok",
                },
                "wireframe_comparison": {"delta_bullish_vs_wireframe": -0.15, "delta_bearish_vs_wireframe": 0.03},
            }
        ],
        "ticker_rollup": [],
    }
    rec = lookup_cron_pcr_recommendation("MU", "2026-06-26", artifact=artifact)
    assert rec["status"] == "ok"
    assert rec["match_type"] == "exact_exp"
    assert rec["suggested_thresholds"]["pcr_volume_bullish_max"] == 0.72


def test_lookup_cron_pcr_recommendation_rollup():
    from services.options_map_cron_stats import lookup_cron_pcr_recommendation

    artifact = {
        "status": "ok",
        "generated_at_utc": "2026-06-16T12:00:00Z",
        "days": 90,
        "ticker_exp_series": [],
        "ticker_rollup": [
            {
                "ticker": "MU",
                "best_expiration_date": "2026-07-18",
                "snapshot_count": 8,
                "pcr_volume_stats": {"p50": 1.0},
                "suggested_thresholds": {"ready": False, "reason_ru": "мало"},
            }
        ],
    }
    rec = lookup_cron_pcr_recommendation("MU", "2026-06-26", artifact=artifact)
    assert rec["status"] == "not_ready"
    assert rec["match_type"] == "ticker_rollup"
    assert rec["effective_expiration_date"] == "2026-07-18"


def test_lookup_cron_pcr_recommendation_missing():
    from services.options_map_cron_stats import lookup_cron_pcr_recommendation

    rec = lookup_cron_pcr_recommendation("MU", "2026-06-26", artifact=None)
    assert rec["status"] == "missing"

    vals = [0.65, 0.72, 0.78, 0.85, 0.95, 1.05, 1.15, 1.25, 1.35, 1.45]
    s = suggest_pcr_thresholds_from_quantiles(vals, min_samples=10)
    assert s["ready"] is True
    assert s["source"] == "quantile_p25_p75"
    assert s["pcr_volume_bullish_max"] <= s["pcr_volume_bearish_min"]

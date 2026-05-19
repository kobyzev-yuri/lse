"""Tests for event_reaction_labeling market regime feature merge."""

from __future__ import annotations

from datetime import date

from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_REGIME,
    enrich_features_with_market_regime,
    event_reaction_numeric_feature_keys,
)


def test_enrich_features_with_market_regime_full():
    base = {
        "feature_builder_version": "quotes_mvp_1",
        "as_of_trade_date": "2026-05-15",
        "ret_1d_log": 0.01,
    }
    regime = {
        "trade_date": date(2026, 5, 15),
        "spy_close": 739.17,
        "ndx_close": 520.1,
        "dia_close": 420.0,
        "vix_close": 18.57,
        "regime_flags": {"vix_regime": "NEUTRAL", "spy_stress_1d": False},
        "features_json": {
            "log_ret_1d_spy": 0.002,
            "log_ret_1d_ndx": 0.003,
            "log_ret_1d_dia": 0.001,
        },
    }
    out = enrich_features_with_market_regime(base, regime)
    assert out["feature_builder_version"] == FEATURE_BUILDER_VERSION_REGIME
    assert out["market_regime_present"] == 1
    assert out["mkt_spy_close"] == 739.17
    assert out["mkt_vix_regime_ord"] == 1.0
    assert out["mkt_log_ret_1d_spy"] == 0.002
    assert out["mkt_spy_stress_1d"] == 0.0


def test_enrich_features_missing_regime():
    base = {"as_of_trade_date": "2026-05-15", "ret_1d_log": 0.01}
    out = enrich_features_with_market_regime(base, None)
    assert out["market_regime_present"] == 0
    assert out["mkt_vix_regime_ord"] == -1.0


def test_numeric_feature_keys_regime():
    keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_REGIME)
    assert "mkt_vix_close" in keys
    assert "ret_1d_log" in keys
    assert "mkt_vix_regime_ord" in keys

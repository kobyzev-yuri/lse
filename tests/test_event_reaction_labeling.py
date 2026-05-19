"""Tests for event_reaction_labeling market regime feature merge and RSI."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_REGIME,
    _rsi_as_of_from_closes,
    build_features_before,
    enrich_features_with_market_regime,
    event_reaction_numeric_feature_keys,
    missing_quote_feature_keys,
)


def _synthetic_quotes_df(n: int = 30) -> pd.DataFrame:
    dates = pd.date_range("2025-01-02", periods=n, freq="B")
    close = 100.0 * np.exp(np.cumsum(np.random.default_rng(42).normal(0, 0.01, n)))
    return pd.DataFrame(
        {
            "d": [d.date() for d in dates],
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1_000_000,
            "rsi": np.nan,
            "volatility_5": np.nan,
        }
    )


def test_rsi_as_of_from_closes_computed():
    df = _synthetic_quotes_df(30)
    closes = df["close"].to_numpy(dtype=float)
    rsi = _rsi_as_of_from_closes(closes, 25)
    assert rsi is not None
    assert 0.0 <= rsi <= 100.0


def test_build_features_before_includes_computed_rsi():
    df = _synthetic_quotes_df(30)
    feats = build_features_before(
        df,
        as_of_idx=25,
        event_d=date(2025, 2, 15),
        feature_builder_version=FEATURE_BUILDER_VERSION_REGIME,
    )
    assert missing_quote_feature_keys(feats) is None
    assert "rsi_as_of" in feats
    assert 0.0 <= float(feats["rsi_as_of"]) <= 100.0


def test_enrich_features_with_market_regime_full():
    base = {
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


def test_missing_quote_feature_keys_detects_gap():
    assert missing_quote_feature_keys({"ret_1d_log": 0.01}) == "missing:ret_5d_log"


def test_numeric_feature_keys_regime():
    keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_REGIME)
    assert "mkt_vix_close" in keys
    assert "rsi_as_of" in keys

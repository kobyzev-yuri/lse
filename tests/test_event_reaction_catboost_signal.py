"""Tests for event_reaction_catboost_signal helpers (no model file required)."""

from services.event_reaction_catboost_signal import (
    _features_to_model_row,
    _runtime_guards,
)
from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_REGIME,
    event_reaction_numeric_feature_keys,
)
from services.trade_effectiveness_analyzer import _compact_report_for_llm


def test_compact_report_strips_trade_effects():
    payload = {
        "meta": {"days": 7, "strategy": "GAME_5M", "trades_analyzed": 3},
        "summary": {"total": 3},
        "trade_effects": [{"trade_id": 1}],
        "catboost_entry_backtest": {"big": True},
        "top_cases": {"top_losses": list(range(20))},
    }
    slim = _compact_report_for_llm(payload)
    assert "trade_effects" not in slim
    assert "catboost_entry_backtest" not in slim
    assert len(slim["top_cases"]["top_losses"]) <= 8


def test_features_to_model_row_regime():
    keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_REGIME)
    feats = {k: 0.1 for k in keys}
    feats["feature_builder_version"] = FEATURE_BUILDER_VERSION_REGIME
    feats["market_regime_present"] = 1.0
    feats["rsi_as_of"] = 55.0
    feats["close_as_of"] = 100.0
    built = _features_to_model_row("AAPL", feats, feature_builder_version=FEATURE_BUILDER_VERSION_REGIME)
    assert built is not None
    row, sym = built
    assert sym == "AAPL"
    assert len(row) == 1 + len(keys)


def test_runtime_guards_disabled_by_default():
    status, note, path, bundle = _runtime_guards()
    assert status == "disabled"
    assert bundle is None

"""Tests for peer spillover runtime inference."""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import numpy as np

from services.event_reaction_labeling import FEATURE_BUILDER_VERSION_EARNINGS
from services.peer_spillover_signal import predict_peer_spillover


@patch("services.peer_spillover_signal._runtime_bundle")
def test_predict_peer_spillover_includes_source_market_phase(mock_bundle):
    model = MagicMock()
    model.predict.return_value = np.array([0.05])
    mock_bundle.return_value = ("ready", "", "/fake/model.cbm", model)

    features = {
        "feature_builder_version": FEATURE_BUILDER_VERSION_EARNINGS,
        "earnings_market_phase": "BEFORE_OPEN",
    }
    captured: dict = {}

    def _capture_pool(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    fake_catboost = ModuleType("catboost")
    fake_catboost.Pool = _capture_pool  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"catboost": fake_catboost}):
        out = predict_peer_spillover(
            source_symbol="MU",
            peer_ticker="NVDA",
            features_before=features,
            edge_weight=0.8,
            relation_type="supply_chain",
        )

    assert out["peer_spillover_ml_status"] == "ok"
    assert out["peer_forward_log_ret_5d_pred"] == 0.05
    names = captured["feature_names"]
    row = captured["data"][0]
    assert row[names.index("source_market_phase")] == "BEFORE_OPEN"

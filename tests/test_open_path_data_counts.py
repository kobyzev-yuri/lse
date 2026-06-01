"""Tests for open_path_data counts helper."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.open_path_classifier_dataset import collect_open_path_data_counts


def test_collect_open_path_data_counts_maps_scalars():
    conn = MagicMock()
    conn.execute.return_value.scalar.side_effect = [19, 231, 250]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    out = collect_open_path_data_counts(engine)
    assert out["premarket_feature_trading_days"] == 19
    assert out["gap_forecast_open_rows"] == 231
    assert out["gap_forecast_premarket_rows"] == 250

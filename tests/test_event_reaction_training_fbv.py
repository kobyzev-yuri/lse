"""Tests for regression feature_builder_version resolution."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_EARNINGS,
    FEATURE_BUILDER_VERSION_REGIME,
    resolve_training_feature_builder_version,
)


def test_resolve_prefers_config_when_rows_exist():
    engine = MagicMock()
    with patch(
        "services.event_reaction_labeling.count_trainable_regression_rows",
        side_effect=lambda _e, dataset_version, feature_builder_version: (
            10 if feature_builder_version == FEATURE_BUILDER_VERSION_REGIME else 0
        ),
    ):
        fbv = resolve_training_feature_builder_version(
            engine,
            dataset_version="v0_expanded_baseline",
            preferred=FEATURE_BUILDER_VERSION_REGIME,
        )
    assert fbv == FEATURE_BUILDER_VERSION_REGIME


def test_resolve_falls_back_to_earnings_v1():
    engine = MagicMock()
    with patch(
        "services.event_reaction_labeling.count_trainable_regression_rows",
        side_effect=lambda _e, dataset_version, feature_builder_version: (
            499 if feature_builder_version == FEATURE_BUILDER_VERSION_EARNINGS else 0
        ),
    ):
        fbv = resolve_training_feature_builder_version(
            engine,
            dataset_version="v0_expanded_baseline",
            preferred=FEATURE_BUILDER_VERSION_REGIME,
        )
    assert fbv == FEATURE_BUILDER_VERSION_EARNINGS

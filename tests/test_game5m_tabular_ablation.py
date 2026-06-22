"""Tests for tabular ablation layer definitions."""
from __future__ import annotations

from services.game5m_entry_bar_dataset import BAR_TRAIN_FULL_NUMERIC_KEYS
from services.game5m_tabular_ablation import (
    ENTRY_ABLATION_TRACKS,
    HOLD_ABLATION_TRACKS,
    HOLD_RECOVERY_KEYS,
)


def test_entry_cumulative_reaches_full():
    keys = ENTRY_ABLATION_TRACKS["E3_full_TNC"]
    assert keys == BAR_TRAIN_FULL_NUMERIC_KEYS


def test_hold_layers_no_duplicate_hour():
    full = HOLD_ABLATION_TRACKS["H3_full"]
    assert full.count("hour_et") == 1


def test_hold_recovery_keys_subset_of_csv_columns():
  # ref_close/entry_price exist in hold CSV builder output
    assert "entry_rsi_5m" in HOLD_RECOVERY_KEYS

"""Tests for GAME_5M continuation ML schema and train helpers (phase 2.1)."""
from __future__ import annotations

from services.game5m_continuation_catboost import (
    continuation_catboost_schema,
    row_vector_from_trade_effect,
)
from services.game5m_continuation_dataset import (
    CONTINUATION_ML_SCHEMA_VERSION,
    continuation_min_extra_upside_pct,
    continuation_promotion_auc_min,
    get_continuation_train_feature_schema,
    row_from_continuation_dataset_dict,
)


def test_continuation_schema_version():
    assert CONTINUATION_ML_SCHEMA_VERSION == "1"


def test_row_from_continuation_dataset_dict():
    row = row_from_continuation_dataset_dict(
        {
            "ticker": "AMD",
            "exit_signal_type": "TAKE_PROFIT",
            "realized_pct": 2.5,
            "minutes_to_exit": 120.0,
            "entry_rsi_5m": 55.0,
            "label_missed_upside": 1,
        }
    )
    assert row is not None
    assert row[0] == "AMD"
    assert row[1] == "TAKE_PROFIT"
    colnames, cat_idx = get_continuation_train_feature_schema()
    assert len(row) == len(colnames)
    assert cat_idx == [0, 1]


def test_row_vector_from_trade_effect():
    row = row_vector_from_trade_effect(
        ticker="NVDA",
        exit_signal="TAKE_PROFIT_SUSPEND",
        realized_pct=1.8,
        hold_minutes=90.0,
        entry_rsi_5m=40.0,
        trade_mfe_pct=3.2,
    )
    assert row is not None
    assert row[0] == "NVDA"
    assert row[1] == "TAKE_PROFIT_SUSPEND"


def test_continuation_catboost_schema_matches_dataset():
    ds_names, ds_cat = get_continuation_train_feature_schema()
    cb_names, cb_cat = continuation_catboost_schema()
    assert ds_names == cb_names
    assert ds_cat == cb_cat


def test_promotion_defaults():
    assert continuation_promotion_auc_min() == 0.55
    assert continuation_min_extra_upside_pct() == 1.0

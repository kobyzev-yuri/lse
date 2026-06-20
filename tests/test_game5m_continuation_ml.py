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


def test_continuation_ml_live_review_counts_telemetry():
    import pandas as pd

    from services.trade_effectiveness_analyzer import TradeEffect, _build_continuation_ml_live_review

    ts = pd.Timestamp("2026-06-01 10:00", tz="America/New_York")
    effects = [
        TradeEffect(
            trade_id=1,
            ticker="AMD",
            entry_ts=ts,
            exit_ts=ts + pd.Timedelta(hours=1),
            hold_minutes=60.0,
            qty=1.0,
            entry_price=100.0,
            exit_price=102.0,
            net_pnl=2.0,
            realized_pct=2.0,
            realized_log_return=0.02,
            exit_signal="TAKE_PROFIT",
            exit_strategy="GAME_5M",
            potential_best_pct=3.5,
            preventable_worst_pct=-0.5,
            missed_upside_pct=1.5,
            avoidable_loss_pct=0.0,
            likely_late_polling=False,
            entry_rsi_5m=50.0,
            entry_vol_5m_pct=1.0,
            entry_momentum_2h_pct=0.5,
            entry_price_forecast_5m_summary=None,
            entry_prob_up=None,
            entry_prob_down=None,
            entry_news_impact=None,
            entry_advice=None,
            entry_decision=None,
            entry_reasoning=None,
            decision_rule_version=None,
            decision_rule_params=None,
            exit_detail=None,
            position_state_v2=None,
            continuation_gate=None,
            continuation_ml={
                "status": "ok",
                "continuation_proba": 0.62,
                "would_defer_by_model": True,
                "would_defer_take": True,
                "log_only": True,
            },
        )
    ]
    out = _build_continuation_ml_live_review(effects)
    assert out["mode"] == "ok"
    assert out["trades_with_continuation_ml"] == 1
    assert out["would_defer_take_count"] == 1
    assert out["sql_console_path"] == "/sql"

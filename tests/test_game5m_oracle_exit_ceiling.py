"""Tests for GAME_5M oracle exit ceiling (plan phase 0.4)."""
from __future__ import annotations

import pandas as pd

from services.trade_effectiveness_analyzer import (
    TradeEffect,
    _build_game5m_oracle_exit_ceiling,
    _oracle_rth_mfe_pct_from_ohlc,
)


def _make_effect(**kwargs) -> TradeEffect:
    ts = pd.Timestamp("2026-06-10 10:00", tz="America/New_York")
    base = dict(
        trade_id=1,
        ticker="AMD",
        entry_ts=ts,
        exit_ts=ts + pd.Timedelta(hours=2),
        hold_minutes=120.0,
        qty=1.0,
        entry_price=100.0,
        exit_price=103.0,
        net_pnl=3.0,
        realized_pct=3.0,
        realized_log_return=0.03,
        exit_signal="TAKE_PROFIT",
        exit_strategy="GAME_5M",
        potential_best_pct=5.0,
        preventable_worst_pct=-0.5,
        missed_upside_pct=2.0,
        avoidable_loss_pct=0.0,
        likely_late_polling=False,
        entry_rsi_5m=50.0,
        entry_vol_5m_pct=1.0,
        entry_momentum_2h_pct=1.0,
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
        continuation_ml=None,
    )
    base.update(kwargs)
    return TradeEffect(**base)


def test_oracle_rth_mfe_pct_picks_session_high():
    ts0 = pd.Timestamp("2026-06-10 10:00", tz="America/New_York")
    df = pd.DataFrame(
        {
            "datetime": [
                ts0,
                ts0 + pd.Timedelta(minutes=5),
                ts0 + pd.Timedelta(minutes=10),
            ],
            "Open": [100.0, 101.0, 102.0],
            "High": [100.5, 105.0, 103.0],
            "Low": [99.5, 100.5, 102.0],
            "Close": [100.2, 104.0, 102.5],
        }
    )
    oracle = _oracle_rth_mfe_pct_from_ohlc(df, ts0, ts0 + pd.Timedelta(minutes=15), 100.0)
    assert oracle is not None
    assert abs(oracle - 5.0) < 0.01


def test_build_oracle_exit_ceiling_aggregates():
    ts = pd.Timestamp("2026-06-10 10:00", tz="America/New_York")
    df = pd.DataFrame(
        {
            "datetime": [ts, ts + pd.Timedelta(minutes=5)],
            "Open": [100.0, 101.0],
            "High": [100.0, 105.0],
            "Low": [99.0, 100.0],
            "Close": [100.0, 104.0],
        }
    )
    effect = _make_effect(realized_pct=3.0)
    out = _build_game5m_oracle_exit_ceiling([effect], {"AMD": df}, strategy="GAME_5M")
    assert out["mode"] == "ok"
    assert out["trades_with_oracle"] == 1
    assert out["pct_captured_mean"] == 60.0  # 3/5 * 100
    assert "TAKE_PROFIT" in out["by_exit_signal"]

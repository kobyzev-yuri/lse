"""Promotion: GAME_5M_CATBOOST_DATASET_VERSION=bar routes fusion through entry_bar_v2."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from services.catboost_5m_signal import (
    attach_catboost_bar_v2_signal,
    attach_catboost_signal,
    catboost_entry_dataset_version,
    finalize_technical_decision_with_catboost,
)


def _sample_d5() -> dict:
    return {
        "decision": "BUY",
        "price": 100.0,
        "high_5d": 110.0,
        "low_5d": 90.0,
        "rsi_5m": 35.0,
        "momentum_2h_pct": 1.2,
        "momentum_rth_today_pct": 0.5,
        "volatility_5m_pct": 0.4,
        "pullback_from_high_pct": 2.0,
        "bars_count": 120,
        "momentum_rth_today_bars": 8,
        "prob_up": 0.55,
        "prob_down": 0.45,
        "market_session": {"now_et": "2026-06-26 10:00:00"},
        "decision_5m_bar_open_et": "2026-06-26 10:00:00",
    }


def test_catboost_entry_dataset_version_bar_aliases(monkeypatch):
    monkeypatch.setenv("GAME_5M_CATBOOST_DATASET_VERSION", "entry_bar_v2")
    assert catboost_entry_dataset_version() == "bar"
    monkeypatch.setenv("GAME_5M_CATBOOST_DATASET_VERSION", "trade")
    assert catboost_entry_dataset_version() == "trade"


@patch("services.catboost_5m_signal._predict_catboost_bar_v2")
def test_attach_catboost_signal_bar_sets_fusion_fields(mock_predict, monkeypatch):
    monkeypatch.setenv("GAME_5M_CATBOOST_DATASET_VERSION", "bar")
    mock_predict.return_value = ("ok", 0.72, "", "/tmp/model.cbm")
    out = _sample_d5()
    attach_catboost_signal(out, "AMD")
    assert out["catboost_signal_status"] == "ok"
    assert out["catboost_entry_proba_good"] == 0.72
    assert out["catboost_entry_proba_good_v2"] == 0.72
    assert out["catboost_dataset_version"] == "bar"
    mock_predict.assert_called_once()
    assert mock_predict.call_args.kwargs["require_log_flag"] is False


@patch("services.catboost_5m_signal._predict_catboost_bar_v2")
def test_attach_catboost_bar_v2_skips_duplicate_after_fusion(mock_predict, monkeypatch):
    monkeypatch.setenv("GAME_5M_CATBOOST_DATASET_VERSION", "bar")
    mock_predict.return_value = ("ok", 0.72, "", "/tmp/model.cbm")
    out = _sample_d5()
    attach_catboost_signal(out, "AMD")
    attach_catboost_bar_v2_signal(out, "AMD")
    assert mock_predict.call_count == 1


@patch("services.catboost_5m_signal._predict_catboost_bar_v2")
def test_finalize_fusion_hold_below_p_uses_bar_proba(mock_predict, monkeypatch):
    monkeypatch.setenv("GAME_5M_CATBOOST_DATASET_VERSION", "bar")
    monkeypatch.setenv("GAME_5M_CATBOOST_FUSION", "hold_if_buy_below_p")
    monkeypatch.setenv("GAME_5M_CATBOOST_HOLD_BELOW_P", "0.45")
    mock_predict.return_value = ("ok", 0.30, "", "/tmp/model.cbm")
    out = _sample_d5()
    attach_catboost_signal(out, "AMD")
    finalize_technical_decision_with_catboost(out)
    assert out["technical_decision_effective"] == "HOLD"
    assert "bar v2" in (out.get("catboost_fusion_note") or "")


@patch("services.catboost_5m_signal._catboost_runtime_guards")
def test_attach_catboost_signal_trade_still_uses_v1(mock_guards, monkeypatch):
    monkeypatch.setenv("GAME_5M_CATBOOST_DATASET_VERSION", "trade")
    mock_guards.return_value = ("disabled", "off", None, None)
    out = _sample_d5()
    attach_catboost_signal(out, "AMD")
    mock_guards.assert_called_once()
    assert out.get("catboost_signal_status") == "disabled"

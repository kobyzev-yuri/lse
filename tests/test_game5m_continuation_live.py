"""Phase 2.4–2.6: continuation live telemetry, multiday guard, apply gate."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from services.game5m_continuation_live import (
    continuation_ml_should_defer_take,
    evaluate_continuation_ml_at_take,
)
from services.multiday_lr_gate import should_block_continuation_take_defer


def test_evaluate_continuation_ml_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_ENABLED", "false")
    out = evaluate_continuation_ml_at_take(
        ticker="AMD",
        exit_signal="TAKE_PROFIT",
        entry_price=100.0,
        exit_price=102.0,
        hold_minutes=60.0,
        d5={"rsi_5m": 55.0},
        entry_ctx={"rsi_5m": 50.0},
    )
    assert out is None


def test_evaluate_continuation_ml_log_only_default(monkeypatch):
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_ENABLED", "true")
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_LOG_ONLY", "true")
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_GATE_MODE", "log_only")
    fake_pred = {"status": "ok", "continuation_proba": 0.72}

    with patch(
        "services.game5m_continuation_live.predict_continuation_missed_upside_proba",
        return_value=fake_pred,
    ), patch(
        "services.game5m_continuation_live.load_continuation_model_meta",
        return_value={"auc_valid": 0.73},
    ), patch(
        "services.game5m_continuation_live.should_block_continuation_take_defer",
        return_value=(False, {"note": "ok"}),
    ):
        out = evaluate_continuation_ml_at_take(
            ticker="AMD",
            exit_signal="TAKE_PROFIT",
            entry_price=100.0,
            exit_price=102.5,
            hold_minutes=90.0,
            d5={"rsi_5m": 60.0, "momentum_2h_pct": 2.0},
            entry_ctx={"rsi_5m": 48.0},
        )
    assert out is not None
    assert out["log_only"] is True
    assert out["would_defer_by_model"] is True
    assert out["would_defer_take"] is True
    assert continuation_ml_should_defer_take(out) is False


def test_multiday_blocks_continuation_defer(monkeypatch):
    monkeypatch.setenv("GAME_5M_MULTIDAY_HOLD_GATE_MODE", "apply")
    d5 = {
        "multiday_lr_horizon_1d_pct_vs_spot": 0.5,
        "multiday_lr_horizon_2d_pct_vs_spot": 0.6,
        "multiday_lr_horizon_3d_pct_vs_spot": 0.4,
        "multiday_lr_forecast_ok": True,
    }
    blocked, meta = should_block_continuation_take_defer(d5, pnl_current_pct=2.0)
    assert blocked is True
    assert meta.get("bullish_multiday") is True


def test_continuation_ml_apply_when_production_ready(monkeypatch):
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_ENABLED", "true")
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_LOG_ONLY", "false")
    monkeypatch.setenv("GAME_5M_CONTINUATION_ML_GATE_MODE", "apply")
    gate = {
        "status": "ok",
        "log_only": False,
        "apply_allowed": True,
        "would_defer_take": True,
        "multiday_block": False,
    }
    assert continuation_ml_should_defer_take(gate) is True
    assert gate.get("applied") is True


def test_continuation_ml_apply_blocked_by_multiday(monkeypatch):
    gate = {
        "status": "ok",
        "log_only": False,
        "apply_allowed": True,
        "would_defer_take": True,
        "multiday_block": True,
        "defer_block_reason": "multiday_hold_apply_bullish",
    }
    assert continuation_ml_should_defer_take(gate) is False
    assert gate.get("apply_skip_reason") == "multiday_hold_apply_bullish"

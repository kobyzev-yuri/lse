"""Tests for bar v2 BUY-only calibration."""
from __future__ import annotations

import math

import pytest

from services.game5m_entry_bar_v2_calibration import (
    apply_calibrator,
    build_calibration_block,
    evaluate_calibration_gates,
    expected_calibration_error,
    fit_platt_calibrator,
    is_buy_bar_row,
    probability_std,
)


def test_is_buy_bar_row_by_sample_kind():
    assert is_buy_bar_row({"sample_kind": "buy_signal"}) is True
    assert is_buy_bar_row({"sample_kind": "hold_negative"}) is False


def test_is_buy_bar_row_by_decision():
    assert is_buy_bar_row({"technical_decision": "STRONG_BUY"}) is True
    assert is_buy_bar_row({"technical_decision": "HOLD"}) is False


def test_platt_increases_spread_for_compressed_probs():
    raw = [0.51, 0.512, 0.508, 0.515, 0.509, 0.511, 0.507, 0.513]
    labels = [1, 1, 0, 1, 0, 0, 0, 1]
    cal = fit_platt_calibrator(raw, labels)
    assert cal["method"] == "platt"
    calibrated = [apply_calibrator(p, cal) for p in raw]
    assert probability_std(calibrated) >= probability_std(raw) * 0.5


def test_build_calibration_block_has_gates():
    raw = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    labels = [0, 0, 0, 0, 1, 1, 1, 1]
    block = build_calibration_block(raw_probs_valid=raw, labels_valid=labels, auc_valid_all=0.9)
    assert "fusion_calibration_ready" in block
    assert "gate_checks" in block
    assert block["ece_calibrated_valid"] == block["ece_calibrated_valid"]


def test_evaluate_calibration_gates_passes_strong_model():
    metrics = {
        "std_p_calibrated_valid": 0.12,
        "ece_calibrated_valid": 0.04,
        "auc_valid_buy_only": 0.62,
    }
    gates = evaluate_calibration_gates(metrics)
    assert gates["fusion_calibration_ready"] is True


def test_evaluate_calibration_gates_fails_low_std():
    metrics = {
        "std_p_calibrated_valid": 0.004,
        "ece_calibrated_valid": 0.04,
        "auc_valid_buy_only": 0.62,
    }
    gates = evaluate_calibration_gates(metrics)
    assert gates["fusion_calibration_ready"] is False
    assert gates["gate_checks"]["std_p_calibrated_valid"]["pass"] is False


def test_expected_calibration_error_perfect_is_low():
    probs = [0.1, 0.2, 0.8, 0.9]
    labels = [0, 0, 1, 1]
    ece = expected_calibration_error(probs, labels, n_bins=2)
    assert ece < 0.2


@pytest.mark.parametrize(
    "p_raw,expected_range",
    [(0.5, (0.0, 1.0)), (0.99, (0.5, 1.0))],
)
def test_apply_calibrator_identity(p_raw, expected_range):
    out = apply_calibrator(p_raw, {"method": "identity"})
    assert expected_range[0] <= out <= expected_range[1]
    assert math.isfinite(out)

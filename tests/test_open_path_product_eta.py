"""Tests for open-path product ETA estimation."""
from __future__ import annotations

from services.open_path_product_eta import estimate_open_path_product_eta


def test_eta_accumulating_phase():
    snap = {
        "open_path_data": {"premarket_feature_trading_days": 18, "gap_forecast_open_rows": 208},
        "open_path_classifier_dataset": {"n_rule_labels": 40, "n_trainable_rows": 35},
    }
    gates = {
        "overall_open_path_classifier_ready": False,
        "open_path_mvp_prerequisites": {"ready": False},
    }
    acc = {
        "premarket_trading_days_in_window": 10,
        "rule_labels_in_window": 25,
        "gap_open_rows_in_window": 80,
    }
    eta = estimate_open_path_product_eta(
        snapshot=snap,
        gates=gates,
        accumulation=acc,
        lookback_days=21,
    )
    assert eta["phase"] == "accumulating_data"
    assert eta["eta_days_calendar_max"] is not None
    assert eta["eta_days_calendar_max"] > 0
    assert "premarket_trading_days" in eta["blocking_bottlenecks"]


def test_eta_product_ready_phase():
    snap = {
        "open_path_data": {"premarket_feature_trading_days": 70, "gap_forecast_open_rows": 250},
        "open_path_classifier_dataset": {"n_rule_labels": 220, "n_trainable_rows": 200},
    }
    gates = {"overall_open_path_classifier_ready": True, "open_path_mvp_prerequisites": {"ready": True}}
    eta = estimate_open_path_product_eta(snapshot=snap, gates=gates, accumulation={})
    assert eta["phase"] == "product_ready"
    assert eta["eta_days_calendar_max"] in (0, None)


def test_eta_quality_tuning_when_data_sufficient():
    snap = {
        "open_path_data": {"premarket_feature_trading_days": 70, "gap_forecast_open_rows": 250},
        "open_path_classifier_dataset": {"n_rule_labels": 220, "n_trainable_rows": 200},
    }
    gates = {
        "overall_open_path_classifier_ready": False,
        "open_path_mvp_prerequisites": {"ready": True},
        "open_path_classifier": {"ready": False, "reasons": ["valid_accuracy<0.35"]},
        "open_path_trading_shadow": {"ready": False, "reasons": ["n_matured<80"]},
    }
    acc = {"premarket_trading_days_in_window": 15, "rule_labels_in_window": 50}
    eta = estimate_open_path_product_eta(snapshot=snap, gates=gates, accumulation=acc, lookback_days=21)
    assert eta["phase"] == "quality_tuning"
    assert "valid_accuracy" in " ".join(eta.get("quality_blockers") or [])

"""Tests for B2 continuation go/no-go gate evaluation."""
from __future__ import annotations

from scripts.run_game5m_b2_continuation_gonogo_review import evaluate_b2_gonogo_gates


def test_b2_gonogo_go_when_all_gates_pass():
    live = {
        "trades_with_continuation_ml": 16,
        "status_counts": {"ok": 14, "skipped": 2},
    }
    backtest = {
        "meta_summary": {"auc_valid": 0.75},
        "delta_log_return_mean": 0.02,
    }
    out = evaluate_b2_gonogo_gates(live, backtest)
    assert out["verdict"] == "go"


def test_b2_gonogo_defer_low_telemetry():
    live = {"trades_with_continuation_ml": 3, "status_counts": {"ok": 3}}
    backtest = {"meta_summary": {"auc_valid": 0.7}, "delta_log_return_mean": 0.01}
    out = evaluate_b2_gonogo_gates(live, backtest)
    assert out["verdict"] == "defer"


def test_b2_gonogo_caution_between_min_and_target():
    live = {"trades_with_continuation_ml": 10, "status_counts": {"ok": 9, "error": 1}}
    backtest = {"meta_summary": {"auc_valid": 0.6}, "delta_log_return_mean": 0.0}
    out = evaluate_b2_gonogo_gates(live, backtest)
    assert out["verdict"] == "caution"


def test_b2_gonogo_no_go_bad_status_share():
    live = {"trades_with_continuation_ml": 12, "status_counts": {"ok": 5, "predict_failed": 7}}
    backtest = {"meta_summary": {"auc_valid": 0.7}, "delta_log_return_mean": 0.01}
    out = evaluate_b2_gonogo_gates(live, backtest)
    assert out["verdict"] == "no_go"

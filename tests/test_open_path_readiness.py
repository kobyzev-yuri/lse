"""Tests for open-path readiness gates."""
from __future__ import annotations

from services.open_path_readiness import (
    build_open_path_gates,
    gate_open_path_classifier,
    gate_open_path_classifier_dataset,
)


def test_dataset_gate_blocks_when_few_rows():
    snap = {
        "open_path_classifier_dataset": {
            "n_rule_labels": 50,
            "n_trainable_rows": 40,
            "n_classes_distinct": 3,
            "sparse_classes_below_min_samples": [],
            "n_gap_open_unlabeled": 5,
            "labels_without_features": 0,
        }
    }
    g = gate_open_path_classifier_dataset(snap)
    assert g["ready"] is False
    assert any("n_rule_labels" in r for r in g["reasons"])


def test_classifier_gate_ok():
    metrics = {
        "status": "ok",
        "metrics": {
            "n_train": 150,
            "valid_accuracy": 0.42,
            "classes": ["a", "b", "c", "d"],
            "holdout_skipped": False,
        },
    }
    g = gate_open_path_classifier(metrics)
    assert g["ready"] is True


def test_overall_product_requires_prerequisites():
    snap = {
        "open_path_classifier_dataset": {
            "n_rule_labels": 250,
            "n_trainable_rows": 220,
            "n_classes_distinct": 6,
            "sparse_classes_below_min_samples": [],
            "n_gap_open_unlabeled": 0,
            "labels_without_features": 0,
        }
    }
    train = {
        "status": "ok",
        "metrics": {"n_train": 180, "valid_accuracy": 0.4, "classes": list(range(6))},
    }
    shadow = {
        "aggregate": {"n_matured": 100, "n_sign_scored": 100, "sign_accuracy": 0.6, "n_class_scored": 100, "class_accuracy": 0.4, "mean_pseudo_pnl_log": 0.001},
        "trading_gate": {"ready": True, "reasons": []},
    }
    gates = build_open_path_gates(snap, train_metrics=train, shadow_report=shadow, prerequisites_ready=False)
    assert gates["overall_open_path_classifier_model_ready"] is True
    assert gates["overall_open_path_classifier_ready"] is False

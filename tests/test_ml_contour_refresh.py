"""Tests for unified ML contour refresh triggers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.ml_contour_refresh import (
    ContourPhase,
    evaluate_retrain_trigger,
    get_contour_spec,
    resolve_phase,
)


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_resolve_phase_accumulating():
    spec = get_contour_spec("game5m_entry")
    assert resolve_phase(spec, product_ready=False, dataset_ready=False, train_ready=False, continuous_enabled=False) == ContourPhase.ACCUMULATING


def test_trigger_train_on_new_units():
    spec = get_contour_spec("game5m_entry")
    log = {"last_apply_at_utc": _iso_hours_ago(1), "last_train_at_utc": _iso_hours_ago(50)}
    t = evaluate_retrain_trigger(
        spec,
        log,
        new_units_since_last_apply=10,
        new_units_since_last_train=10,
        dataset_ready=True,
        train_ready=True,
    )
    assert t.should_apply_data is True
    assert t.should_train is True
    assert "new_units_train>=8" in t.reasons[0] or any("new_units" in r for r in t.reasons)


def test_trigger_skip_train_when_insufficient_delta():
    spec = get_contour_spec("game5m_entry")
    log = {"last_apply_at_utc": _iso_hours_ago(1), "last_train_at_utc": _iso_hours_ago(2)}
    t = evaluate_retrain_trigger(
        spec,
        log,
        new_units_since_last_apply=2,
        new_units_since_last_train=2,
        dataset_ready=True,
        train_ready=True,
    )
    assert t.should_apply_data is False
    assert t.should_train is False


def test_trigger_staleness_fallback():
    spec = get_contour_spec("earnings_grid")
    log = {"last_apply_at_utc": _iso_hours_ago(200), "last_train_at_utc": _iso_hours_ago(200)}
    t = evaluate_retrain_trigger(
        spec,
        log,
        new_units_since_last_apply=0,
        new_units_since_last_train=0,
        dataset_ready=True,
        train_ready=True,
    )
    assert t.should_apply_data is True
    assert t.should_train is True


def test_should_write_catboost_model_continuous_prod():
    from services.ml_contour_runner import should_write_catboost_model

    assert should_write_catboost_model(
        cli_dry_run=False,
        do_train=True,
        full_train=False,
        readiness_train_mode="dry_run",
        phase="continuous_prod",
        continuous_enabled=True,
    )
    assert not should_write_catboost_model(
        cli_dry_run=False,
        do_train=True,
        full_train=False,
        readiness_train_mode="dry_run",
        phase="accumulating_data",
        continuous_enabled=True,
    )
    assert should_write_catboost_model(
        cli_dry_run=False,
        do_train=True,
        full_train=True,
        readiness_train_mode="dry_run",
        phase="accumulating_data",
        continuous_enabled=False,
    )


def test_continuous_prod_trains_on_apply():
    spec = get_contour_spec("open_path")
    log = {"last_apply_at_utc": _iso_hours_ago(3)}
    t = evaluate_retrain_trigger(
        spec,
        log,
        new_units_since_last_apply=1,
        new_units_since_last_train=1,
        product_ready=True,
        dataset_ready=True,
        train_ready=True,
    )
    assert t.phase == ContourPhase.CONTINUOUS.value
    assert t.should_apply_data is True
    assert t.should_train is True
    assert "continuous_prod" in t.reasons


def test_force_full():
    spec = get_contour_spec("open_path")
    t = evaluate_retrain_trigger(spec, {}, force_full=True)
    assert t.should_apply_data is True
    assert t.should_train is True
    assert t.should_full_shadow is True

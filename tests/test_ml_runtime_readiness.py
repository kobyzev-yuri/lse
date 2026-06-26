"""ML runtime readiness diagnostics."""
from __future__ import annotations

from services.game5m_entry_bar_dataset import BAR_TRAIN_NUMERIC_KEYS, resolve_bar_v2_feature_mode
from services.ml_runtime_readiness import (
    _telemetry_blockers,
    build_ml_runtime_readiness_diagnostics,
    probe_all_entry_shadow_contours,
)


def test_resolve_bar_v2_feature_mode_tech():
    meta = {"feature_names": ["ticker", *BAR_TRAIN_NUMERIC_KEYS]}
    assert resolve_bar_v2_feature_mode(meta) == "tech"


def test_telemetry_blockers_feature_mismatch():
    blockers = _telemetry_blockers(
        contour_id="catboost_entry_bar_v2",
        config_enabled=True,
        total=20,
        ok_n=0,
        mismatch_n=11,
        missing_n=9,
        with_proba=0,
        status_rows=[{"status": "feature_mismatch", "n": 11}],
    )
    assert any("feature_mismatch" in b for b in blockers)


def test_build_diagnostics_without_engine():
    out = build_ml_runtime_readiness_diagnostics(None, days=7)
    assert "live_entry_probes" in out
    assert "priority_blockers" in out
    assert out["overall_runtime_health"] in ("healthy", "blocked_schema", "blocked_telemetry", "collecting")


def test_probe_all_entry_shadow_contours_structure():
    probes = probe_all_entry_shadow_contours()
    assert len(probes) == 3
    ids = {p["contour_id"] for p in probes}
    assert "catboost_entry_bar_v2" in ids

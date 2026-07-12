"""CatBoost bar v2 calibration in predict path."""
from __future__ import annotations

from services.catboost_5m_signal import _apply_bar_v2_calibration


def test_apply_bar_v2_calibration_uses_calibrated_when_ready():
    meta = {
        "fusion_calibration_ready": True,
        "calibration": {
            "calibrator": {"method": "platt", "a": 2.0, "b": 0.0},
            "fusion_calibration_ready": True,
        },
    }
    p_fusion, p_raw, note, active = _apply_bar_v2_calibration(0.51, meta)
    assert p_raw == 0.51
    assert p_fusion is not None
    assert p_fusion != p_raw
    assert active is True
    assert "calibrated" in note


def test_apply_bar_v2_calibration_raw_when_gates_fail():
    meta = {
        "fusion_calibration_ready": False,
        "calibration": {
            "calibrator": {"method": "platt", "a": 2.0, "b": 0.0},
            "fusion_calibration_ready": False,
        },
    }
    p_fusion, p_raw, note, active = _apply_bar_v2_calibration(0.51, meta)
    assert p_fusion == 0.51
    assert p_raw == 0.51
    assert active is False
    assert "not_ready" in note

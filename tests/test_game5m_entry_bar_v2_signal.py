"""Bar-level entry CatBoost v2 shadow telemetry (phase 1.6)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.catboost_5m_signal import build_catboost_bar_v2_feature_row
from services.trade_effectiveness_analyzer import (
    _build_game5m_entry_bar_dataset_stats,
    _build_game5m_entry_model_v2_status,
    _trust_level_game5m_entry_bar_v2,
)


def test_build_catboost_bar_v2_feature_row():
    colnames, row = build_catboost_bar_v2_feature_row(
        "AMD",
        {
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
        },
    )
    assert colnames[0] == "ticker"
    assert row[0] == "AMD"
    assert len(row) == len(colnames)
    assert row[colnames.index("rsi_5m")] == pytest.approx(35.0)


def test_trust_level_entry_bar_v2_below_promotion():
    trust = _trust_level_game5m_entry_bar_v2({"auc_valid": 0.5495, "n_valid": 1925})
    assert trust["entry_bar_v2_trust_level"] == "low"
    assert "0.55" in trust["entry_bar_v2_trust_reason"]


def test_build_entry_bar_dataset_stats_missing_file(tmp_path, monkeypatch):
    missing = tmp_path / "missing_stats.json"
    monkeypatch.setenv("GAME_5M_ENTRY_BAR_DATASET_STATS_PATH", str(missing))
    stats = _build_game5m_entry_bar_dataset_stats()
    assert stats.get("skip_reason") == "no_stats_file"
    assert stats.get("stats_path") == str(missing)


def test_build_entry_bar_dataset_stats_reads_json(tmp_path, monkeypatch):
    stats_path = tmp_path / "game5m_entry_bar_dataset_stats.json"
    stats_path.write_text(
        json.dumps({"n_rows": 9625, "y_entry_good_rate": 0.34, "tb_label_counts": {"upper": 1}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GAME_5M_ENTRY_BAR_DATASET_STATS_PATH", str(stats_path))
    out = _build_game5m_entry_bar_dataset_stats()
    assert out["n_rows"] == 9625
    assert out["meets_min_rows"] is True


def test_build_entry_model_v2_status_structure(monkeypatch):
    monkeypatch.delenv("GAME_5M_CATBOOST_V2_MODEL_PATH", raising=False)
    status = _build_game5m_entry_model_v2_status()
    assert "model_path" in status
    assert status.get("prod_v1_unchanged") is True
    assert status.get("promotion_auc_min") == 0.55

"""Tests for unified trust arbiter."""
from __future__ import annotations

from services.unified_trust_arbiter import (
    _contour_digest_lines,
    _data_volume_ru,
    build_unified_trust_arbiter,
    format_operator_digest_ru,
    multiday_lr_reality_from_wf_artifact,
    recommended_gate_mode,
    resolve_multiday_lr_reality_check,
    trust_label_from_score,
)


def test_trust_label_thresholds():
    assert trust_label_from_score(0.80) == "high"
    assert trust_label_from_score(0.60) == "medium"
    assert trust_label_from_score(0.30) == "low"
    assert trust_label_from_score(0.10) == "insufficient"


def test_recommended_gate_mode_respects_l2():
    assert recommended_gate_mode("high", l2_ready=True) == "apply"
    assert recommended_gate_mode("high", l2_ready=False) == "log_only"
    assert recommended_gate_mode("medium", l2_ready=True) == "caution"
    assert recommended_gate_mode("insufficient", l2_ready=True) == "none"


def test_multiday_lr_reality_from_wf_artifact():
    wf = {
        "generated_at_utc": "2026-06-16T00:00:00Z",
        "verdict": "caution",
        "rationale_ru": "test",
        "live_feature_set": "v3nm",
        "v3nm_pooled": {
            "1": {"n_points_sum": 220, "mean_sign_accuracy": 0.54, "mean_rmse_oos_log_across_tickers": 0.09},
        },
    }
    mlr = multiday_lr_reality_from_wf_artifact(wf)
    assert mlr["mode"] == "ok"
    assert mlr["walkforward_production_verdict"] == "caution"
    assert mlr["pooled_by_horizon"]["1"]["n_points_sum"] == 220


def test_resolve_multiday_prefers_report_then_wf():
    report = {"multiday_lr_reality_check": {"mode": "ok", "walkforward_production_verdict": "ready"}}
    mlr = resolve_multiday_lr_reality_check(report, project_root=None)
    assert mlr["walkforward_production_verdict"] == "ready"
    mlr2 = resolve_multiday_lr_reality_check(
        {},
        project_root=None,
    )
    assert isinstance(mlr2, dict)


def test_build_unified_trust_arbiter_minimal():
    arb = build_unified_trust_arbiter(project_root=None, report={})
    assert arb["arbiter_version"] == "trust_v1"
    assert "GAME_5M" in arb["surfaces"]
    assert "EARNINGS" in arb["surfaces"]
    earn = arb["surfaces"]["EARNINGS"]
    assert "context_slices" in earn
    assert arb["operator_digest_ru"].startswith("LSE Trust")
    assert isinstance(arb["decision_stack_weights"], dict)


def test_format_operator_digest_includes_surfaces():
    arb = build_unified_trust_arbiter(project_root=None, report={})
    text = format_operator_digest_ru(arb)
    assert "GAME_5M" in text
    assert "EARNINGS" in text
    assert "Итог:" in text
    assert "• " in text
    assert "Док: docs/GAME_5M_DECISION_ARCHITECTURE.md" in text


def test_data_volume_ru_shows_shortfall():
    assert "Мало данных" in _data_volume_ru(44, 50, unit="событий")
    assert "≥6" in _data_volume_ru(44, 50, unit="событий")
    assert "✓" in _data_volume_ru(80, 80, unit="сделок")


def test_contour_digest_lines_human_readable():
    lines = _contour_digest_lines(
        {
            "contour_id": "catboost_entry_5m",
            "trust_label": "medium",
            "trust_score": 0.61,
            "recommended_gate_mode": "log_only",
            "n_matured": 49,
            "T_hit": 0.52,
            "T_hit_insufficient": True,
            "conclusion_ru": "catboost_entry: medium 0.61, log_only, AUC 0.52",
        }
    )
    joined = "\n".join(lines)
    assert "CatBoost вход" in joined
    assert "49/80" in joined
    assert "telemetry" in joined


def test_contour_digest_lines_entry_bar_v2_shadow():
    lines = _contour_digest_lines(
        {
            "contour_id": "catboost_entry_bar_v2",
            "trust_label": "low",
            "trust_score": 0.42,
            "recommended_gate_mode": "log_only",
            "n_matured": 1925,
            "dataset_n_rows": 9625,
            "T_hit": 0.5495,
            "T_hit_insufficient": False,
            "conclusion_ru": "CatBoost entry bar v2 (shadow): low 0.42, log_only, AUC valid 0.55, dataset 9625 rows",
        }
    )
    joined = "\n".join(lines)
    assert "bar v2" in joined
    assert "shadow" in joined.lower() or "log_only" in joined
    assert "9625" in joined or "1925" in joined


def test_build_unified_trust_includes_entry_bar_v2(tmp_path, monkeypatch):
    q = tmp_path / "ml_data_quality"
    ds = tmp_path / "datasets"
    models = tmp_path / "models"
    q.mkdir(parents=True)
    ds.mkdir(parents=True)
    models.mkdir(parents=True)
    metrics = {
        "status": "ok",
        "dataset": "bar",
        "auc_valid": 0.5495,
        "n_valid": 1925,
        "n_train": 7700,
        "n_total": 9625,
    }
    (q / "last_game5m_entry_bar_v2_train_metrics.json").write_text(
        __import__("json").dumps(metrics),
        encoding="utf-8",
    )
    (ds / "game5m_entry_bar_dataset_stats.json").write_text(
        __import__("json").dumps({"n_rows": 9625}),
        encoding="utf-8",
    )
    (models / "game5m_entry_catboost_v2.cbm").write_bytes(b"")
    (models / "game5m_entry_catboost_v2.meta.json").write_text(
        __import__("json").dumps(metrics),
        encoding="utf-8",
    )

    def _paths(_root=None):
        return (
            q / "last_game5m_entry_bar_v2_train_metrics.json",
            models / "game5m_entry_catboost_v2.meta.json",
            ds / "game5m_entry_bar_dataset_stats.json",
        )

    monkeypatch.setattr(
        "services.unified_trust_arbiter._default_entry_bar_v2_metrics_paths",
        _paths,
    )
    arb = build_unified_trust_arbiter(project_root=tmp_path, report={})
    game = arb["surfaces"]["GAME_5M"]["contours"]
    ids = [c.get("contour_id") for c in game]
    assert "catboost_entry_bar_v2" in ids
    bar = next(c for c in game if c.get("contour_id") == "catboost_entry_bar_v2")
    assert bar.get("recommended_gate_mode") == "log_only"
    assert "bar v2" in format_operator_digest_ru(arb).lower()

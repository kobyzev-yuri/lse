import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.ml_data_quality_report import (
    _collect_earnings_intelligence_readiness_bundle,
    build_ml_data_quality_report,
    load_fresh_json,
)


def test_load_fresh_json_hit_and_stale(tmp_path: Path):
    p = tmp_path / "report.json"
    p.write_text(json.dumps({"ok": True}), encoding="utf-8")
    data, meta = load_fresh_json(p, max_age_sec=3600)
    assert data == {"ok": True}
    assert meta["cache"] == "hit"

    old = time.time() - 7200
    import os

    os.utime(p, (old, old))
    data2, meta2 = load_fresh_json(p, max_age_sec=3600)
    assert data2 is None
    assert meta2["reason"] == "stale"


def test_build_uses_report_daily_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    q_dir = tmp_path / "ml_data_quality"
    q_dir.mkdir(parents=True)
    rd = q_dir / "report_daily.json"
    cached = {"report_version": "1.1", "knowledge_base": {"rows_total": 42}}
    rd.write_text(json.dumps(cached), encoding="utf-8")

    monkeypatch.setattr(
        "services.ml_data_quality_report.default_report_daily_path",
        lambda _root: rd,
    )
    monkeypatch.setattr(
        "services.ml_data_quality_report._report_cache_max_age_sec",
        lambda: 3600,
    )

    engine = MagicMock()
    with patch("services.ml_data_quality_report.collect_knowledge_base_stats") as kb:
        bundle = build_ml_data_quality_report(
            project_root=tmp_path,
            engine=engine,
            dataset_paths=[],
            use_cache=True,
        )
    kb.assert_not_called()
    assert bundle["knowledge_base"]["rows_total"] == 42
    assert bundle["cache_meta"]["cache"] == "hit"


def test_readiness_bundle_reads_cache_without_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    readiness_path = tmp_path / "last_earnings_intelligence_readiness.json"
    readiness_path.write_text(
        json.dumps(
            {
                "snapshot": {"labels": 100},
                "gates": {"overall_grid_ready": False},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "services.earnings_intelligence_readiness.default_readiness_metrics_path",
        lambda _root: readiness_path,
    )
    monkeypatch.setattr(
        "services.ml_data_quality_report._readiness_cache_max_age_sec",
        lambda: 3600,
    )

    with patch(
        "services.earnings_intelligence_readiness.write_earnings_intelligence_readiness"
    ) as write_fn:
        out = _collect_earnings_intelligence_readiness_bundle(
            MagicMock(),
            tmp_path,
            force_refresh=False,
        )
    write_fn.assert_not_called()
    assert out["file"]["cache"] == "hit"
    assert out["gates"]["overall_grid_ready"] is False

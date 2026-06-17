"""Tests for earnings trust runtime (post-mortem → decision_stack)."""
from __future__ import annotations

from datetime import date, timedelta

from services.earnings_trust_runtime import (
    build_earnings_trust_runtime,
    find_postmortem_for_ticker,
)


def _sample_row(*, symbol: str = "ORCL", event_date: str | None = None, peer: str | None = None):
    ev = event_date or (date.today() - timedelta(days=7)).isoformat()
    row = {
        "symbol": symbol,
        "event_date": ev,
        "postmortem_version": "earnings_postmortem_v2",
        "context": {"scenario_class": "gap_up", "alignment": "conflict"},
        "models": {
            "scenario_sign": {"hit": False, "predicted_scenario": "gap_up"},
            "regression_5d": {"fact": -0.08, "sign_hit": False},
            "peer_spillover": [],
        },
        "fusion": {"would_have_blocked": True, "conviction": "low", "alignment": "conflict"},
        "fusion_outcome": {"fact_was_bad": True, "block_was_correct": True},
    }
    if peer:
        row["models"]["peer_spillover"] = [
            {"peer": peer, "pred": 0.03, "fact_5d": 0.01, "sign_hit": True},
        ]
    return row


def test_find_postmortem_source(monkeypatch):
    rows = [_sample_row(symbol="ORCL")]
    monkeypatch.setattr(
        "services.earnings_trust_runtime.load_postmortem_rows",
        lambda project_root=None: rows,
    )
    found = find_postmortem_for_ticker("ORCL")
    assert found is not None
    assert found["runtime_role"] == "source"


def test_find_postmortem_peer(monkeypatch):
    rows = [_sample_row(symbol="ORCL", peer="NVDA")]
    monkeypatch.setattr(
        "services.earnings_trust_runtime.load_postmortem_rows",
        lambda project_root=None: rows,
    )
    found = find_postmortem_for_ticker("NVDA")
    assert found is not None
    assert found["runtime_role"] == "peer"
    assert found["runtime_peer_block"]["peer"] == "NVDA"


def test_build_runtime_would_downgrade_on_bad_fusion(monkeypatch):
    rows = [_sample_row(symbol="ORCL")]
    monkeypatch.setattr(
        "services.earnings_trust_runtime.load_postmortem_rows",
        lambda project_root=None: rows,
    )
    monkeypatch.setattr(
        "services.earnings_trust_runtime.load_trust_metrics",
        lambda project_root=None: {"degradation": {"degrading": False}},
    )
    monkeypatch.setattr(
        "services.earnings_trust_runtime._load_arbiter",
        lambda project_root=None: {
            "surfaces": {
                "EARNINGS": {
                    "contours": [
                        {"contour_id": "earnings_scenario", "trust_label": "medium", "trust_score": 0.55},
                        {"contour_id": "peer_spillover", "trust_label": "low", "trust_score": 0.35},
                    ]
                }
            }
        },
    )
    rt = build_earnings_trust_runtime("ORCL")
    assert rt["active"] is True
    assert rt["would_downgrade"] is True
    assert rt["strength"] < 0

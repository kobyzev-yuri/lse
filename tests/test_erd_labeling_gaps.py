"""Tests for ERD labeling gap audit."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from services.earnings_intelligence_readiness import gate_labeling_gaps
from services.erd_labeling_gaps import audit_erd_labeling_gaps


def test_gate_labeling_gaps_under_threshold():
    g = gate_labeling_gaps(
        {
            "no_quotes": 2,
            "anchor_unresolved": 3,
            "thresholds": {"no_quotes_max": 5, "anchor_unresolved_max": 15},
        }
    )
    assert g["ready"] is True
    assert g["reasons"] == []


def test_gate_labeling_gaps_over_threshold():
    g = gate_labeling_gaps(
        {
            "no_quotes": 8,
            "anchor_unresolved": 20,
            "thresholds": {"no_quotes_max": 5, "anchor_unresolved_max": 15},
        }
    )
    assert g["ready"] is False
    assert "no_quotes>5" in g["reasons"]
    assert "anchor_unresolved>15" in g["reasons"]


@patch("services.erd_labeling_gaps.get_event_reaction_symbol_allowlist", return_value=["META"])
@patch("services.event_reaction_labeling.compute_row_labeling")
def test_audit_excludes_future_events(mock_label, _allowlist):
    mock_label.return_value = (None, None, None, "anchor_unresolved")
    row = {
        "id": 1,
        "symbol": "META",
        "event_time_et": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "knowledge_base_id": 10,
    }
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.all.return_value = [row]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    out = audit_erd_labeling_gaps(engine, symbols=["META"], limit=10)
    assert out["future_events_excluded"] == 1
    assert out["no_quotes"] == 0
    assert out["anchor_unresolved"] == 0
    mock_label.assert_not_called()


@patch("services.erd_labeling_gaps.get_event_reaction_symbol_allowlist", return_value=["NBIS"])
@patch("services.event_reaction_labeling.compute_row_labeling")
def test_audit_counts_past_no_quotes(mock_label, _allowlist):
    mock_label.return_value = (None, None, None, "no_quotes")
    row = {
        "id": 2,
        "symbol": "NBIS",
        "event_time_et": datetime(2021, 4, 27, tzinfo=timezone.utc),
        "knowledge_base_id": 11,
    }
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.all.return_value = [row]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    out = audit_erd_labeling_gaps(engine, symbols=["NBIS"], limit=10)
    assert out["no_quotes"] == 1
    assert out["anchor_unresolved"] == 0

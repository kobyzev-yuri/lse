"""Tests for autoprep daily digest."""
from __future__ import annotations

from services.earnings_autoprep_digest import format_autoprep_digest_message


def test_format_autoprep_digest_includes_gates():
    msg = format_autoprep_digest_message(
        {
            "pending_calendar_events": 2,
            "steps": {"materials_sync": 0, "materials_ingest": 0, "materials_extract": 0},
            "readiness": {
                "overall_grid_ready": True,
                "overall_peer_spillover_ready": True,
                "overall_earnings_autoprep_ready": False,
                "earnings_autoprep": {
                    "reasons": ["shadow_n_matured<50"],
                    "llm_scenario_labels": 42,
                    "shadow_n_matured": 44,
                },
            },
        }
    )
    assert "pending=2" in msg
    assert "Autoprep gate: false" in msg
    assert "shadow 44/50" in msg

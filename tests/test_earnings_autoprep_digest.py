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
            },
        }
    )
    assert "pending_calendar=2" in msg
    assert "autoprep_ready=False" in msg

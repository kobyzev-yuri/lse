"""Tests for permanent earnings material ingest skip rules."""
from __future__ import annotations

from services.earnings_material_ingest_skip import (
    permanent_ingest_skip_reason,
    row_should_skip_ingest,
)


def test_arm_edge_media_server_skipped():
    reason = permanent_ingest_skip_reason(
        symbol="ARM",
        source_url="https://edge.media-server.com/mmc/arm/q1-2026",
    )
    assert reason is not None
    assert "edge.media-server.com" in reason


def test_normal_url_not_skipped():
    assert (
        permanent_ingest_skip_reason(
            symbol="META",
            source_url="https://investor.fb.com/static-files/earnings.pdf",
        )
        is None
    )


def test_repeat_short_text_on_blocked_url():
    reason = permanent_ingest_skip_reason(
        symbol="ARM",
        source_url="https://edge.media-server.com/foo",
        parse_status="failed",
        parse_error="short_text:0",
    )
    assert reason is not None
    assert "ingest_skip" in reason


def test_repeat_short_text_pdf_skipped():
    reason = permanent_ingest_skip_reason(
        symbol="ARM",
        source_url="https://investors.arm.com/static-files/q1.pdf",
        parse_status="failed",
        parse_error="short_text:0",
    )
    assert reason is not None
    assert "short_text_pdf" in reason


def test_row_should_skip_ingest():
    row = {
        "symbol": "ARM",
        "source_url": "https://edge.media-server.com/mmc/x",
        "parse_status": "registered",
    }
    assert row_should_skip_ingest(row) is not None

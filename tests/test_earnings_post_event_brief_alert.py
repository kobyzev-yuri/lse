"""Tests for earnings post-event brief Telegram formatting."""
from __future__ import annotations

from services.earnings_post_event_brief_alert import format_brief_telegram


def test_format_brief_telegram_ok():
    text = format_brief_telegram(
        {
            "status": "ok",
            "symbol": "CIEN",
            "event_date": "2026-05-28",
            "scenario": {"id": "beat_selloff_pullback", "confidence": "medium"},
            "management_tone": "cautious",
            "peer_spillover_outcomes": [
                {"ticker": "LITE", "forward_log_ret_5d": 0.012},
            ],
        },
        base_url="http://example.com",
    )
    assert "CIEN" in text
    assert "beat_selloff_pullback" in text
    assert "LITE" in text


def test_format_brief_telegram_not_found():
    text = format_brief_telegram({"status": "not_found", "symbol": "X", "event_date": "2026-01-01"})
    assert "not_found" in text

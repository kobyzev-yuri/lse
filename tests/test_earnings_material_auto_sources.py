"""Tests for SEC/Fool earnings material auto-discovery."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from services.earnings_material_auto_sources import (
    _edgar_forms_for_symbol,
    auto_materials_for_event,
    sec_edgar_filings_near_date,
)


def _tsm_submissions_payload() -> dict:
    return {
        "filings": {
            "recent": {
                "form": ["6-K", "8-K", "6-K"],
                "filingDate": ["2026-04-16", "2026-03-01", "2026-01-16"],
                "accessionNumber": [
                    "0001193125-26-123456",
                    "0001193125-26-000001",
                    "0001193125-26-654321",
                ],
                "primaryDocument": [
                    "d123456d6k.htm",
                    "d8k.htm",
                    "d654321d6k.htm",
                ],
            }
        }
    }


def test_edgar_forms_for_tsm_uses_6k():
    assert _edgar_forms_for_symbol("TSM") == ("6-K",)
    assert _edgar_forms_for_symbol("MU") == ("8-K",)


def test_sec_edgar_filings_near_date_tsm_picks_6k_not_8k():
    event_date = date(2026, 4, 15)
    with patch(
        "services.earnings_material_auto_sources._load_sec_submissions",
        return_value=_tsm_submissions_payload(),
    ):
        rows = sec_edgar_filings_near_date("TSM", event_date, window_days=10)
    assert len(rows) == 1
    assert rows[0].symbol == "TSM"
    assert "6-K" in rows[0].title
    assert rows[0].meta["auto_source"] == "sec_6k"
    assert "d123456d6k.htm" in rows[0].source_url


def test_auto_materials_for_event_tsm_includes_catalog_without_network():
    event_date = date(2026, 4, 15)
    with patch(
        "services.earnings_material_auto_sources._load_sec_submissions",
        return_value=None,
    ), patch(
        "services.earnings_material_auto_sources.fool_transcript_near_date",
        return_value=None,
    ):
        rows = auto_materials_for_event(
            "TSM",
            event_date,
            include_sec=True,
            include_sec_exhibits=False,
            include_fool=False,
        )
    assert any(cm.material_type == "ir_event_page" for cm in rows)
    assert all(cm.symbol == "TSM" for cm in rows)


def test_sec_edgar_filings_unknown_ticker_returns_empty():
    assert sec_edgar_filings_near_date("ZZZZ", date(2026, 4, 15)) == []


def test_sec_edgar_exhibit_skips_when_index_unreachable():
    event_date = date(2026, 4, 15)
    mock_resp = MagicMock(status_code=404, text="")
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with patch(
        "services.earnings_material_auto_sources._load_sec_submissions",
        return_value=_tsm_submissions_payload(),
    ), patch(
        "services.earnings_material_auto_sources._sec_session",
        return_value=mock_sess,
    ):
        from services.earnings_material_auto_sources import sec_edgar_exhibit_materials_near_date

        rows = sec_edgar_exhibit_materials_near_date("TSM", event_date, window_days=10)
    assert rows == []

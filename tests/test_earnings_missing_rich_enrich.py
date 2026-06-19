"""Tests for missing_rich auto-enrich helpers."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from services.earnings_missing_rich_enrich import (
    enrich_missing_rich_materials,
    missing_rich_calendar_events,
)


def test_missing_rich_calendar_events_filters_pipeline_reason():
    engine = MagicMock()
    rows = [
        {"symbol": "AMD", "event_date": date(2026, 2, 3), "pipeline_reason": "missing_rich_material"},
        {"symbol": "MSFT", "event_date": date(2026, 4, 29), "pipeline_reason": "pending_extract"},
    ]
    with patch(
        "services.earnings_missing_rich_enrich.load_materials_pipeline_calendar_events",
        return_value=rows,
    ):
        out = missing_rich_calendar_events(engine, since=date(2026, 1, 1))
    assert len(out) == 1
    assert out[0]["symbol"] == "AMD"


def test_enrich_missing_rich_registers_catalog_rows():
    engine = MagicMock()
    ev = {"symbol": "NBIS", "event_date": date(2026, 2, 12), "pipeline_reason": "missing_rich_material"}
    fake_cm = MagicMock(
        symbol="NBIS",
        event_date=date(2026, 2, 12),
        fiscal_period="Q4 2025",
        material_type="press_release",
        source_name="Nebius IR",
        source_url="https://nebius.com/newsroom/example",
        title="PR",
    )
    with patch(
        "services.earnings_missing_rich_enrich.missing_rich_calendar_events",
        return_value=[ev],
    ), patch(
        "services.earnings_missing_rich_enrich.resolve_kb_id_for_earnings_event",
        return_value=64639,
    ), patch(
        "services.earnings_missing_rich_enrich.catalog_for_event",
        return_value=[fake_cm],
    ), patch(
        "services.earnings_missing_rich_enrich.auto_materials_for_event",
        return_value=(),
    ), patch(
        "services.earnings_missing_rich_enrich._upsert_catalog_material",
        return_value=999,
    ), patch(
        "services.earnings_missing_rich_enrich.reclassify_ir_event_transcripts",
        return_value=[],
    ):
        summary = enrich_missing_rich_materials(engine, since=date(2026, 1, 1))
    assert summary["registered"] == 1
    assert summary["enriched_keys"] == [("NBIS", date(2026, 2, 12))]

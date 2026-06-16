from datetime import date, timedelta

from services.earnings_event_date_match import (
    expand_event_date_keys,
    material_matches_kb_event_date_sql,
)


def test_expand_event_date_keys_includes_neighbors():
    keys = {("ARM", date(2026, 5, 5))}
    expanded = expand_event_date_keys(keys, tolerance_days=1)
    assert expanded == {
        ("ARM", date(2026, 5, 4)),
        ("ARM", date(2026, 5, 5)),
        ("ARM", date(2026, 5, 6)),
    }


def test_expand_event_date_keys_zero_tolerance():
    keys = {("META", date(2026, 4, 29))}
    assert expand_event_date_keys(keys, tolerance_days=0) == keys


def test_material_matches_kb_event_date_sql_tolerance():
    sql = material_matches_kb_event_date_sql(tolerance_days=1)
    assert "ABS(em.event_date - kb.ts::date) <= 1" in sql


def test_material_matches_kb_event_date_sql_exact():
    sql = material_matches_kb_event_date_sql(tolerance_days=0)
    assert "em.event_date = kb.ts::date" in sql
    assert "ABS" not in sql

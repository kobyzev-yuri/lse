"""SQL console preset catalog."""
from __future__ import annotations

from services.sql_console import validate_readonly_sql
from services.sql_console_presets import (
    SQL_CONSOLE_PRESET_GROUPS,
    sql_console_preset_by_id,
    sql_console_presets_for_ui,
)


def test_preset_groups_non_empty():
    groups = sql_console_presets_for_ui()
    assert len(groups) >= 3
    assert any(g["id"] == "ml_continuation" for g in groups)


def test_all_presets_valid_select():
    for group in SQL_CONSOLE_PRESET_GROUPS:
        for preset in group["presets"]:
            try:
                validate_readonly_sql(preset["sql"])
            except ValueError as e:
                raise AssertionError(f"{preset['id']}: {e}") from e


def test_continuation_preset_lookup():
    p = sql_console_preset_by_id("continuation_ml_recent")
    assert p is not None
    assert "continuation_ml" in p["sql"]
    wait = sql_console_preset_by_id("continuation_ml_wait_dashboard")
    assert wait is not None
    assert "days_since_last_take" in wait["sql"]

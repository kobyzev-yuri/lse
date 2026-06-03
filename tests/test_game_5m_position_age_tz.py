"""trade_history.ts naive = Moscow; position age must not treat now() as Eastern."""

from __future__ import annotations

from datetime import datetime

from services.game_5m import _position_age_for_exit


def test_position_age_moscow_naive_forty_minutes():
    pos = {
        "entry_ts": datetime(2026, 6, 3, 18, 40, 0),
    }
    ref = datetime(2026, 6, 3, 19, 20, 0)
    age = _position_age_for_exit(pos, simulation_time=ref)
    assert 39.0 <= age.total_seconds() / 60.0 <= 41.0

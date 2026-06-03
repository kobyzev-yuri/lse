"""Stale/early_derisk must win over TAKE_PROFIT when close PnL is negative but bar_high hits take."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from services.game_5m import should_close_position


def _config_side_effect(env: dict):
    def _get(key, default=None):
        if key in env:
            return env[key]
        return default

    return _get


@pytest.fixture
def lite_like_position():
    entry_ts = datetime.now() - timedelta(hours=30)
    return {
        "ticker": "LITE",
        "entry_price": 1002.06,
        "entry_ts": entry_ts,
        "quantity": 9,
    }


def test_early_derisk_before_take_on_negative_close(lite_like_position):
    """Prod-like: −2.3% close, bar_high hits take — expect early_derisk (stale threshold looser)."""
    env = {
        "GAME_5M_STALE_REVERSAL_EXIT_ENABLED": "true",
        "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES": "570",
        "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT": "-8.0",
        "GAME_5M_EARLY_DERISK_ENABLED": "true",
        "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES": "240",
        "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT": "-2.2",
        "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW": "-0.2",
        "GAME_5M_TAKE_PROFIT_PCT": "5.0",
        "GAME_5M_TAKE_PROFIT_PCT_LITE": "4.5",
        "GAME_5M_TAKE_PROFIT_MIN_PCT": "2.0",
        "GAME_5M_STOP_LOSS_ENABLED": "false",
        "GAME_5M_EXIT_ONLY_TAKE": "true",
    }
    with patch("services.game_5m.get_config_value", side_effect=_config_side_effect(env)):
        should, sig, detail = should_close_position(
            lite_like_position,
            current_decision="HOLD",
            current_price=978.58,
            momentum_2h_pct=-5.2,
            bar_high=1049.53,
            bar_low=974.52,
        )
    assert should is True
    assert sig == "TIME_EXIT_EARLY"
    assert detail == "early_derisk"


def test_stale_before_take_on_negative_close(lite_like_position):
    """LITE-like: bar_high triggers take, close is a loss — expect TIME_EXIT_EARLY stale_reversal."""
    env = {
        "GAME_5M_STALE_REVERSAL_EXIT_ENABLED": "true",
        "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES": "390",
        "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT": "-1.5",
        "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW": "0.0",
        "GAME_5M_TAKE_PROFIT_PCT": "5.0",
        "GAME_5M_TAKE_PROFIT_PCT_LITE": "4.5",
        "GAME_5M_TAKE_PROFIT_MIN_PCT": "2.0",
        "GAME_5M_STOP_LOSS_ENABLED": "false",
        "GAME_5M_EXIT_ONLY_TAKE": "false",
        "GAME_5M_HANGER_TUNE_APPLY_TAKE": "false",
    }
    with patch("services.game_5m.get_config_value", side_effect=_config_side_effect(env)):
        should, sig, detail = should_close_position(
            lite_like_position,
            current_decision="HOLD",
            current_price=978.58,
            momentum_2h_pct=-5.2,
            bar_high=1049.53,
            bar_low=974.52,
        )
    assert should is True
    assert sig == "TIME_EXIT_EARLY"
    assert detail == "stale_reversal"


def test_take_still_fires_on_positive_close_with_bar_high(lite_like_position):
    env = {
        "GAME_5M_STALE_REVERSAL_EXIT_ENABLED": "true",
        "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES": "390",
        "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT": "-1.5",
        "GAME_5M_TAKE_PROFIT_PCT": "5.0",
        "GAME_5M_TAKE_PROFIT_PCT_LITE": "4.5",
        "GAME_5M_TAKE_PROFIT_MIN_PCT": "2.0",
        "GAME_5M_STOP_LOSS_ENABLED": "false",
        "GAME_5M_EXIT_ONLY_TAKE": "false",
    }
    with patch("services.game_5m.get_config_value", side_effect=_config_side_effect(env)):
        should, sig, detail = should_close_position(
            lite_like_position,
            current_decision="HOLD",
            current_price=1048.0,
            momentum_2h_pct=3.0,
            bar_high=1050.0,
        )
    assert should is True
    assert sig == "TAKE_PROFIT"
    assert detail == ""


def test_negative_close_young_position_can_still_take(lite_like_position):
    """Stale not met (too young); wick take still allowed when stale disabled or age low."""
    ref = datetime(2026, 6, 3, 14, 0, 0)
    young = {
        **lite_like_position,
        "entry_ts": ref - timedelta(hours=2),
    }
    env = {
        "GAME_5M_STALE_REVERSAL_EXIT_ENABLED": "false",
        "GAME_5M_EARLY_DERISK_ENABLED": "false",
        "GAME_5M_TAKE_PROFIT_PCT": "5.0",
        "GAME_5M_TAKE_PROFIT_PCT_LITE": "4.5",
        "GAME_5M_TAKE_PROFIT_MIN_PCT": "2.0",
        "GAME_5M_STOP_LOSS_ENABLED": "false",
        "GAME_5M_EXIT_ONLY_TAKE": "false",
    }
    with patch("services.game_5m.get_config_value", side_effect=_config_side_effect(env)):
        should, sig, _ = should_close_position(
            young,
            current_decision="HOLD",
            current_price=978.58,
            momentum_2h_pct=-1.0,
            bar_high=1049.53,
            simulation_time=ref,
        )
    assert should is True
    assert sig == "TAKE_PROFIT"

"""
Совместимость: перенаправление на game_5m с тикером SNDK.

Игра по 5m поддерживает любую быструю бумагу через services.game_5m (ticker передаётся явно).
Здесь — обёртки с ticker='SNDK' для старых вызовов без указания тикера.
"""

from __future__ import annotations

from services.game_5m import (
    GAME_5M_STRATEGY,
    GAME_NOTIONAL_USD,
    COMMISSION_RATE,
    MAX_POSITION_DAYS,
    get_open_position as _get_open_position,
    record_entry as _record_entry,
    close_position as _close_position,
    get_recent_results as _get_recent_results,
    should_close_position,
)


def get_open_position(ticker: str = "SNDK"):
    return _get_open_position(ticker)


def record_entry(price, signal_type, reasoning=None, ticker: str = "SNDK"):
    return _record_entry(ticker, price, signal_type, reasoning)


def close_position(exit_price, exit_signal_type, ticker: str = "SNDK"):
    return _close_position(ticker, exit_price, exit_signal_type)


def get_recent_results(ticker: str = "SNDK", limit: int = 20):
    return _get_recent_results(ticker, limit)

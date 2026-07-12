"""PnL helper for bar v2 calibration backtest."""
from __future__ import annotations

from types import SimpleNamespace

from scripts.run_game5m_entry_bar_v2_buy_calibration import _realized_pct_from_trade


def test_realized_pct_from_trade():
    t = SimpleNamespace(entry_price=100.0, quantity=10.0, net_pnl=50.0)
    assert _realized_pct_from_trade(t) == 5.0


def test_realized_pct_from_trade_loss():
    t = SimpleNamespace(entry_price=50.0, quantity=4.0, net_pnl=-10.0)
    assert _realized_pct_from_trade(t) == -5.0


def test_realized_pct_from_trade_zero_cost():
    t = SimpleNamespace(entry_price=0.0, quantity=10.0, net_pnl=10.0)
    assert _realized_pct_from_trade(t) is None

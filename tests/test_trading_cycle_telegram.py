"""Telegram text for portfolio trade notifications."""

from scripts.trading_cycle_cron import _portfolio_trade_pnl_suffix


def test_pnl_suffix_sell():
    assert (
        _portfolio_trade_pnl_suffix({"side": "SELL", "pnl_usd": 830.5, "pnl_pct": 3.456})
        == ", PnL $+830.50 (+3.46%)"
    )


def test_pnl_suffix_sell_negative():
    assert (
        _portfolio_trade_pnl_suffix({"side": "SELL", "pnl_usd": -120.0, "pnl_pct": -1.2})
        == ", PnL $-120.00 (-1.20%)"
    )


def test_pnl_suffix_usd_only():
    assert _portfolio_trade_pnl_suffix({"side": "SELL", "pnl_usd": 50.0}) == ", PnL $+50.00"


def test_pnl_suffix_buy_empty():
    assert _portfolio_trade_pnl_suffix({"side": "BUY", "pnl_pct": 5.0}) == ""


def test_pnl_suffix_missing():
    assert _portfolio_trade_pnl_suffix({"side": "SELL"}) == ""

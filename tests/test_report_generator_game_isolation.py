"""Portfolio и GAME_5M на одном тикере — отдельные ledger в report_generator."""

import pandas as pd

from report_generator import compute_closed_trade_pnls, compute_open_positions


def _row(**kwargs):
    base = {
        "commission": 0.0,
        "signal_type": "BUY",
        "sentiment_at_trade": None,
        "take_profit": None,
        "stop_loss": None,
        "mfe": None,
        "mae": None,
        "context_json": None,
    }
    base.update(kwargs)
    return base


def test_open_positions_isolated_by_strategy_on_same_ticker():
    trades = pd.DataFrame(
        [
            _row(id=1, ts="2026-04-29", ticker="TER", side="BUY", quantity=16, price=381.15, strategy_name="Portfolio"),
            _row(id=2, ts="2026-05-27", ticker="TER", side="BUY", quantity=26, price=374.66, strategy_name="GAME_5M", signal_type="STRONG_BUY"),
        ]
    )
    open_pos = compute_open_positions(trades)
    by_strategy = {(p.strategy_name, p.quantity, round(p.entry_price, 2)) for p in open_pos}
    assert ("Portfolio", 16.0, 381.15) in by_strategy
    assert ("GAME_5M", 26.0, 374.66) in by_strategy
    assert len(open_pos) == 2


def test_game5m_sell_does_not_reduce_portfolio_ledger():
    trades = pd.DataFrame(
        [
            _row(id=1, ts="2026-04-29", ticker="TER", side="BUY", quantity=16, price=381.15, strategy_name="Portfolio"),
            _row(id=2, ts="2026-05-27", ticker="TER", side="BUY", quantity=26, price=374.66, strategy_name="GAME_5M", signal_type="STRONG_BUY"),
            _row(id=3, ts="2026-06-02", ticker="TER", side="SELL", quantity=26, price=388.66, strategy_name="GAME_5M", signal_type="TAKE_PROFIT_SUSPEND"),
        ]
    )
    open_pos = compute_open_positions(trades)
    assert len(open_pos) == 1
    assert open_pos[0].strategy_name == "Portfolio"
    assert open_pos[0].quantity == 16.0
    assert abs(open_pos[0].entry_price - 381.15) < 0.01

    closed = compute_closed_trade_pnls(trades)
    assert len(closed) == 1
    assert closed[0].entry_strategy == "GAME_5M"
    assert closed[0].exit_strategy == "GAME_5M"
    assert abs(closed[0].entry_price - 374.66) < 0.01

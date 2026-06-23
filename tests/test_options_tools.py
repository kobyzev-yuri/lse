# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from services.options_calculator import compute_put_strategy
from services.options_chain_sentiment import analyze_options_chain


def test_pure_put_breakeven_and_max_loss():
    r = compute_put_strategy(
        strategy="pure_put",
        spot=100.0,
        contracts=1,
        long_strike=100.0,
        long_premium=5.0,
    )
    assert r["entry_cost_usd"] == 500.0
    assert r["breakeven"] == 95.0
    assert r["max_loss_usd"] == 500.0
    flat = r["scenarios"][0]
    assert flat["drop_pct"] == 0.0
    assert flat["pnl_usd"] == -500.0
    assert "Максимальный убыток" in flat["status_ru"]


def test_put_spread_max_profit_at_deep_drop():
    r = compute_put_strategy(
        strategy="put_spread",
        spot=200.0,
        contracts=2,
        long_strike=200.0,
        long_premium=8.0,
        short_strike=180.0,
        short_premium=3.0,
    )
    assert r["entry_cost_usd"] == 1000.0  # (8-3)*100*2
    assert r["max_profit_usd"] == 3000.0  # (20-5)*100*2
    deep = [s for s in r["scenarios"] if s["drop_pct"] == -20.0][0]
    assert deep["pnl_usd"] == 3000.0
    assert deep["status_ru"] == "Максимальная прибыль"


def test_sentiment_bearish_on_put_heavy_oi():
    contracts = []
    for k in (185.0, 190.0, 195.0):
        contracts.append(
            {
                "strike": k,
                "contract_type": "call",
                "volume": 100,
                "open_interest": 500,
                "underlying_price": 189.0,
            }
        )
        contracts.append(
            {
                "strike": k,
                "contract_type": "put",
                "volume": 800,
                "open_interest": 3000,
                "underlying_price": 189.0,
            }
        )
    a = analyze_options_chain(contracts, spot=189.0)
    assert a["sentiment_label"] == "BEARISH"
    assert a["totals"]["pcr_open_interest"] > 1.0


def test_put_spread_requires_higher_long_strike():
    with pytest.raises(ValueError):
        compute_put_strategy(
            strategy="put_spread",
            spot=100.0,
            contracts=1,
            long_strike=90.0,
            long_premium=5.0,
            short_strike=100.0,
            short_premium=2.0,
        )

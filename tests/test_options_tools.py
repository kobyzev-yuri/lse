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
    assert r["max_profit_usd"] == 9500.0  # 100*100 - 500 at S→0
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


def test_calculator_demo_examples_list():
    from services.options_calculator import list_calculator_demo_examples

    ex = list_calculator_demo_examples()
    assert len(ex) >= 3
    assert ex[0]["preview"]["entry_cost_usd"] > 0
    assert "scenarios" in ex[0]["preview"]


def test_fetch_spot_yfinance_requires_ticker():
    from services.options_calculator_prefill import fetch_spot_yfinance

    r = fetch_spot_yfinance("")
    assert r["status"] == "error"


def test_load_calendar_empty_ticker():
    from unittest.mock import MagicMock

    from services.options_calculator_prefill import load_ticker_earnings_calendar

    r = load_ticker_earnings_calendar(MagicMock(), "")
    assert r["status"] == "error"


def test_load_calendar_picks_nearest_future(monkeypatch):
    from datetime import date
    from unittest.mock import MagicMock

    from services.options_calculator_prefill import load_ticker_earnings_calendar

    class FakeResult:
        def mappings(self):
            return self

        def all(self):
            return [
                {"knowledge_base_id": 1, "event_date": date(2025, 1, 15), "source": "Yahoo", "report_timing": None},
                {"knowledge_base_id": 2, "event_date": date(2099, 6, 25), "source": "Yahoo", "report_timing": "AMC"},
            ]

    conn = MagicMock()
    conn.execute.return_value = FakeResult()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    monkeypatch.setattr(
        "services.options_calculator_prefill._suggest_expiration",
        lambda t, e: ("2099-06-28", "polygon_reference"),
    )

    r = load_ticker_earnings_calendar(engine, "MU")
    assert r["status"] == "ok"
    assert r["suggested_earnings_date"] == "2099-06-25"
    assert r["suggested_expiration_date"] == "2099-06-28"
    assert r["pick_reason"] == "nearest_future"

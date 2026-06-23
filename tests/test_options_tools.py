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


def test_yfinance_rows_to_contracts():
    import pandas as pd

    from services.yfinance_options import _rows_to_contracts

    df = pd.DataFrame(
        [
            {
                "contractSymbol": "MU260626P00190000",
                "strike": 190.0,
                "bid": 8.0,
                "ask": 9.0,
                "lastPrice": 8.5,
                "volume": 120,
                "openInterest": 5000,
                "impliedVolatility": 0.45,
            }
        ]
    )
    out = _rows_to_contracts(
        df,
        contract_type="put",
        expiration_date="2026-06-26",
        underlying="MU",
        underlying_price=189.0,
    )
    assert len(out) == 1
    assert out[0]["strike"] == 190.0
    assert out[0]["open_interest"] == 5000
    assert out[0]["contract_type"] == "put"


def test_compute_chain_totals():
    from services.options_chain_sentiment import compute_chain_totals

    contracts = [
        {"contract_type": "call", "volume": 100, "open_interest": 500},
        {"contract_type": "put", "volume": 300, "open_interest": 900},
    ]
    t = compute_chain_totals(contracts)
    assert t["pcr_volume"] == 3.0
    assert t["pcr_open_interest"] == 1.8


def test_llm_interpret_requires_data():
    from services.options_sentiment_llm import interpret_options_chain_report

    r = interpret_options_chain_report({"status": "error", "error": "x"})
    assert r["status"] == "error"


def test_format_llm_anthropic_500_message():
    pytest.importorskip("openai")
    from services.llm_service import format_llm_provider_error

    msg = format_llm_provider_error(
        "Error code: 500 - AnthropicException Internal Server Error anthropic/claude-sonnet-4-6"
    )
    assert "Anthropic" in msg
    assert "LLM_COMPARE_MODELS" in msg


def test_build_analyzer_llm_attempts_includes_compare(monkeypatch):
    pytest.importorskip("openai")
    from services import llm_service

    monkeypatch.setattr(llm_service, "resolve_analyzer_llm_base_model", lambda: ("https://x/v1", "primary"))
    monkeypatch.setattr(llm_service, "load_config", lambda: {"LLM_COMPARE_MODELS": "gpt-5.4-mini"})
    monkeypatch.setattr(
        llm_service,
        "parse_compare_models",
        lambda cfg: [("https://api.proxyapi.ru/openai/v1", "gpt-5.4-mini")],
    )
    attempts = llm_service.build_analyzer_llm_attempts()
    assert attempts[0] == ("https://x/v1", "primary")
    assert len(attempts) == 2


def test_yfinance_sentiment_report(monkeypatch):
    from services.options_chain_sentiment import build_yfinance_chain_sentiment_report

    fake_contracts = [
        {
            "strike": 190.0,
            "contract_type": "call",
            "volume": 100,
            "open_interest": 500,
            "underlying_price": 189.0,
        },
        {
            "strike": 190.0,
            "contract_type": "put",
            "volume": 800,
            "open_interest": 3000,
            "underlying_price": 189.0,
        },
    ]

    monkeypatch.setattr(
        "services.yfinance_options.fetch_yfinance_option_expirations",
        lambda t: ["2026-06-26"],
    )
    monkeypatch.setattr(
        "services.yfinance_options.fetch_yfinance_option_chain",
        lambda t, expiration_date: {
            "status": "ok",
            "underlying_price": 189.0,
            "contracts": fake_contracts,
            "calls_count": 1,
            "puts_count": 1,
        },
    )

    r = build_yfinance_chain_sentiment_report("MU", expiration_date="2026-06-26")
    assert r["source"] == "yfinance"
    assert r["sentiment_label"] == "BEARISH"
    assert r["totals"]["pcr_open_interest"] > 1.0
    assert r["totals_full_chain"]["pcr_volume"] == 8.0

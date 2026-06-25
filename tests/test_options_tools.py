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


def test_sentiment_zero_oi_uses_volume_barriers():
    contracts = []
    for k in (1040.0, 1050.0, 1060.0):
        contracts.append(
            {
                "strike": k,
                "contract_type": "call",
                "volume": 1000 + int(k),
                "open_interest": 0,
                "underlying_price": 1051.0,
            }
        )
        contracts.append(
            {
                "strike": k,
                "contract_type": "put",
                "volume": 2000 + int(k),
                "open_interest": 0,
                "underlying_price": 1051.0,
            }
        )
    a = analyze_options_chain(contracts, spot=1051.0)
    assert a["oi_available"] is False
    assert a["barriers_mode"] == "volume"
    assert a["max_pain_strike"] is None
    assert a["key_strikes_oi"] == []
    assert len(a["key_strikes_volume"]) > 0
    assert a["key_strikes_volume"][0]["total_volume"] > 0
    assert a["totals"]["pcr_open_interest"] is None


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


def test_put_spread_stale_strike_warning():
    r = compute_put_strategy(
        strategy="put_spread",
        spot=1084.71,
        contracts=1,
        long_strike=189.0,
        long_premium=8.5,
        short_strike=180.0,
        short_premium=7.2,
    )
    assert r["input_warning_ru"]
    assert "OTM" in r["input_warning_ru"] or "ниже spot" in r["input_warning_ru"]


def test_put_spread_sane_strikes():
    r = compute_put_strategy(
        strategy="put_spread",
        spot=1084.71,
        contracts=2,
        long_strike=1100.0,
        long_premium=61.0,
        short_strike=1050.0,
        short_premium=42.0,
    )
    assert r.get("input_warning_ru") is None
    deep = [s for s in r["scenarios"] if s["drop_pct"] == -20.0][0]
    assert deep["pnl_usd"] > 0


def test_llm_interpret_requires_data():
    from services.options_sentiment_llm import interpret_options_chain_report

    r = interpret_options_chain_report({"status": "error", "error": "x"})
    assert r["status"] == "error"


def test_compact_report_for_llm_includes_days_to_expiration():
    from datetime import date

    from services.options_sentiment_llm import _compact_report_for_llm, _days_to_expiration

    assert _days_to_expiration("2026-06-26", as_of=date(2026, 6, 14)) == 12
    payload = _compact_report_for_llm(
        {
            "status": "ok",
            "ticker": "MU",
            "source": "polygon",
            "expiration_date": "2026-06-26",
            "spot": 1213.0,
            "totals": {"pcr_volume": 0.5},
            "totals_full_chain": {"pcr_open_interest": 1.88},
            "key_strikes_oi": [{"strike": 1200, "total_oi": 14060}],
        }
    )
    assert payload["calendar_days_to_expiration"] == _days_to_expiration("2026-06-26")
    assert payload["as_of_date"] == date.today().isoformat()
    assert "expiration_note_ru" in payload
    assert payload["totals_window"]["pcr_volume"] == 0.5


def test_llm_calculator_interpret_requires_data():
    from services.options_calculator_llm import interpret_calculator_result

    r = interpret_calculator_result({"error": "x"})
    assert r["status"] == "error"


def test_mid_option_price():
    from services.options_calculator_prefill import _mid_option_price

    assert _mid_option_price(10.0, 12.0, None) == 11.0
    assert _mid_option_price(None, None, 8.5) == 8.5


def test_calculator_polygon_prefill_monkeypatch(monkeypatch):
    from services.options_calculator_prefill import fetch_calculator_polygon_prefill

    monkeypatch.setattr(
        "services.polygon_options.fetch_option_expiration_dates",
        lambda t: ["2026-06-26"],
    )
    monkeypatch.setattr(
        "services.polygon_options.polygon_options_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "services.polygon_options.fetch_options_chain_snapshot",
        lambda ticker, expiration_date=None, limit=250: {
            "status": "ok",
            "underlying_price": 1090.0,
            "spot_source": "stocks_snapshot",
            "contracts": [
                {
                    "contract_type": "put",
                    "strike": 1090.0,
                    "bid": 56.0,
                    "ask": 58.0,
                    "last": 57.0,
                    "volume": 10,
                    "open_interest": 100,
                },
                {
                    "contract_type": "put",
                    "strike": 1050.0,
                    "bid": 40.0,
                    "ask": 42.0,
                    "last": 41.0,
                    "volume": 5,
                    "open_interest": 50,
                },
            ],
        },
    )

    r = fetch_calculator_polygon_prefill("MU", expiration_date="2026-06-26", strategy="put_spread")
    assert r["status"] == "ok"
    assert r["source"] == "polygon"
    assert r["long_strike"] == 1090.0
    assert r["short_strike"] == 1050.0


def test_calculator_yfinance_prefill_monkeypatch(monkeypatch):
    from services.options_calculator_prefill import fetch_calculator_yfinance_prefill

    monkeypatch.setattr(
        "services.options_calculator_prefill.fetch_spot_yfinance",
        lambda t: {"status": "ok", "ticker": "MU", "spot": 1090.0, "price_kind": "info.regularMarketPrice"},
    )
    monkeypatch.setattr(
        "services.yfinance_options.fetch_yfinance_option_expirations",
        lambda t: ["2026-06-26"],
    )
    monkeypatch.setattr(
        "services.yfinance_options.fetch_yfinance_option_chain",
        lambda t, expiration_date: {
            "status": "ok",
            "contracts": [
                {
                    "contract_type": "put",
                    "strike": 1090.0,
                    "bid": 56.0,
                    "ask": 58.0,
                    "last": 57.0,
                    "volume": 10,
                    "open_interest": 100,
                },
                {
                    "contract_type": "put",
                    "strike": 1050.0,
                    "bid": 40.0,
                    "ask": 42.0,
                    "last": 41.0,
                    "volume": 5,
                    "open_interest": 50,
                },
            ],
        },
    )
    r = fetch_calculator_yfinance_prefill("MU", expiration_date="2026-06-26", strategy="put_spread")
    assert r["status"] == "ok"
    assert r["long_strike"] == 1090.0
    assert r["long_premium"] == 57.0
    assert r["short_strike"] == 1050.0


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


def test_money_map_chart_bars_oi_filter():
    from services.options_money_map import _filter_chart_bars_for_display

    bars = [
        {"strike": 990.0, "put_oi": 10, "call_oi": 5, "total_oi": 15},
        {"strike": 1000.0, "put_oi": 8000, "call_oi": 200, "total_oi": 8200},
        {"strike": 1050.0, "put_oi": 3000, "call_oi": 100, "total_oi": 3100},
        {"strike": 1100.0, "put_oi": 50, "call_oi": 5000, "total_oi": 5050},
        {"strike": 1200.0, "put_oi": 5, "call_oi": 8, "total_oi": 13},
    ]
    kept, meta = _filter_chart_bars_for_display(bars)
    strikes = [b["strike"] for b in kept]
    assert 990.0 not in strikes
    assert 1200.0 not in strikes
    assert 1000.0 in strikes
    assert meta["bars_raw"] == 5
    assert meta["bars_shown"] < 5
    assert meta["oi_threshold"] >= 200


def test_money_map_one_liner():
    from services.options_money_map import build_summary_one_liner

    s = build_summary_one_liner(
        spot=1050.0,
        support=[{"strike": 1000.0, "oi": 8000}],
        resistance=[{"strike": 1180.0, "oi": 5000}],
        flow_label="BULLISH",
        flow_ru="свежее активнее call",
        oi_available=True,
        pcr_volume=0.72,
        pcr_volume_bullish_max=0.87,
        pcr_volume_bearish_min=1.15,
    )
    assert "плита" in s
    assert "PCR vol 0.72" in s
    assert "≤0.87" in s and "≥1.15" in s
    assert "1 000" in s or "1000" in s
    assert "1 180" in s or "1180" in s
    assert "call" in s.lower() or "рост" in s


def test_money_map_report_monkeypatch(monkeypatch):
    from services.options_money_map import build_money_map_report

    monkeypatch.setattr(
        "services.polygon_options.polygon_options_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "services.polygon_options.fetch_option_expiration_dates",
        lambda t: ["2026-06-26"],
    )
    monkeypatch.setattr(
        "services.polygon_options.fetch_options_chain_snapshot",
        lambda ticker, expiration_date=None, limit=250: {
            "status": "ok",
            "underlying_price": 1050.0,
            "spot_source": "stocks_snapshot",
            "contracts": [
                {"contract_type": "put", "strike": 1000.0, "open_interest": 8000, "volume": 100},
                {"contract_type": "put", "strike": 1050.0, "open_interest": 2000, "volume": 50},
                {"contract_type": "call", "strike": 1100.0, "open_interest": 3000, "volume": 80},
                {"contract_type": "call", "strike": 1180.0, "open_interest": 5000, "volume": 120},
            ],
        },
    )
    r = build_money_map_report("MU", expiration_date="2026-06-26")
    assert r["status"] == "ok"
    assert r["support_plate"][0]["strike"] == 1000.0
    assert r["resistance_ceiling"][0]["strike"] == 1180.0
    assert r["summary_one_liner_ru"]
    bd = r.get("one_liner_breakdown") or {}
    assert bd.get("is_template_not_llm") is True
    assert len(bd.get("steps") or []) >= 5
    assert bd.get("intro_ru")
    assert bd.get("caveats_ru")
    assert bd.get("assembled_ru")
    assert r["pcr_thresholds"]["pcr_volume_bullish_max"] == 0.87
    assert len(r["chart_bars"]) >= 2


def test_resolve_pcr_vol_thresholds_override():
    from services.options_money_map import resolve_pcr_vol_thresholds

    th = resolve_pcr_vol_thresholds(ticker="MU", pcr_volume_bullish_max=0.8, pcr_volume_bearish_min=1.25)
    assert th["source"] == "ui_override"
    assert th["pcr_volume_bullish_max"] == 0.8
    assert th["pcr_volume_bearish_min"] == 1.25
    assert th["calibrated"] is False


def test_flow_label_custom_thresholds():
    from services.options_money_map import _flow_label

    label, _ = _flow_label(0.9, pcr_volume_bullish_max=0.95, pcr_volume_bearish_min=1.2)
    assert label == "BULLISH"
    label2, _ = _flow_label(0.9, pcr_volume_bullish_max=0.85, pcr_volume_bearish_min=1.2)
    assert label2 == "NEUTRAL"


def test_money_map_report_custom_pcr_thresholds(monkeypatch):
    from services.options_money_map import build_money_map_report

    monkeypatch.setattr("services.polygon_options.polygon_options_available", lambda: True)
    monkeypatch.setattr("services.polygon_options.fetch_option_expiration_dates", lambda t: ["2026-06-26"])
    monkeypatch.setattr(
        "services.polygon_options.fetch_options_chain_snapshot",
        lambda ticker, expiration_date=None, limit=250: {
            "status": "ok",
            "underlying_price": 1050.0,
            "contracts": [
                {"contract_type": "put", "strike": 1000.0, "open_interest": 8000, "volume": 50},
                {"contract_type": "call", "strike": 1100.0, "open_interest": 3000, "volume": 100},
            ],
        },
    )
    r = build_money_map_report("MU", expiration_date="2026-06-26", pcr_volume_bullish_max=0.95, pcr_volume_bearish_min=1.2)
    assert r["pcr_thresholds"]["pcr_volume_bullish_max"] == 0.95
    assert r["flow_label"] == "BULLISH"  # pcr 0.5


def test_money_map_one_liner_breakdown():
    from services.options_money_map import build_one_liner_breakdown

    bd = build_one_liner_breakdown(
        sym="MU",
        exp="2026-06-26",
        spot_f=1182.0,
        spot_source="stocks_snapshot",
        source="polygon",
        snapshot_date=None,
        support=[
            {"strike": 1100.0, "oi": 12000},
            {"strike": 1000.0, "oi": 9000},
            {"strike": 950.0, "oi": 8000},
        ],
        resistance=[
            {"strike": 1200.0, "oi": 15000},
            {"strike": 1250.0, "oi": 11000},
            {"strike": 1300.0, "oi": 7000},
        ],
        flow_label="BULLISH",
        flow_ru="свежее активнее call (ставки на рост)",
        pcr_vol=0.72,
        call_vol=5000,
        put_vol=3600,
        call_oi=40000,
        put_oi=35000,
        scope={"strike_lo": 945.6, "strike_hi": 1418.4, "contracts_in_window": 120, "contracts_raw": 200},
        strike_window_pct=0.20,
        summary_one_liner_ru="…",
        pcr_thresholds={"pcr_volume_bullish_max": 0.87, "pcr_volume_bearish_min": 1.15, "source": "default"},
    )
    flow = next(s for s in bd["steps"] if s["id"] == "flow")
    assert "0.72" in (flow.get("formula_ru") or "")
    assert bd["thresholds"]["pcr_volume_bullish_max"] == 0.87
    put = next(s for s in bd["steps"] if s["id"] == "put_plate")
    assert "$950" in put["result_ru"] and "$1 100" in put["result_ru"]


def test_money_map_snapshot_assembly():
    from services.options_money_map import _assemble_money_map_report

    contracts = [
        {"contract_type": "put", "strike": 1000.0, "open_interest": 8000, "volume": 100},
        {"contract_type": "call", "strike": 1100.0, "open_interest": 3000, "volume": 80},
    ]
    r = _assemble_money_map_report(
        "MU",
        "2026-06-26",
        contracts=contracts,
        spot_f=1050.0,
        source="snapshot",
        available_expirations=["2026-06-26"],
        strike_window_pct=0.20,
        snapshot_date="2026-06-24",
        available_snapshot_dates=["2026-06-24"],
    )
    assert r["status"] == "ok"
    assert r["is_live"] is False
    assert r["snapshot_date"] == "2026-06-24"
    assert r["source"] == "snapshot"
    assert r["available_snapshot_dates"] == ["2026-06-24"]


def test_money_map_from_snapshot_db(monkeypatch):
    from services.options_money_map import build_money_map_report

    monkeypatch.setattr(
        "services.polygon_options.polygon_options_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "services.polygon_options.fetch_option_expiration_dates",
        lambda t: ["2026-06-26"],
    )
    monkeypatch.setattr(
        "services.options_money_map.list_oi_snapshot_dates",
        lambda ticker, expiration_date=None: ["2026-06-24"],
    )
    monkeypatch.setattr(
        "services.options_money_map._load_snapshot_contracts",
        lambda ticker, snapshot_date, expiration_date: {
            "status": "ok",
            "spot": 1050.0,
            "contracts": [
                {"contract_type": "put", "strike": 1000.0, "open_interest": 8000, "volume": 100},
                {"contract_type": "call", "strike": 1100.0, "open_interest": 3000, "volume": 80},
            ],
        },
    )
    r = build_money_map_report("MU", expiration_date="2026-06-26", snapshot_date="2026-06-24")
    assert r["status"] == "ok"
    assert r["snapshot_date"] == "2026-06-24"
    assert r["support_plate"][0]["strike"] == 1000.0


def test_options_oi_watchlist_filters_macro(monkeypatch):
    monkeypatch.setattr(
        "services.ticker_groups.get_tickers_game_5m",
        lambda: ["MU", "SNDK"],
    )
    monkeypatch.setattr(
        "services.ticker_groups.get_tickers_for_portfolio_game",
        lambda: ["AMD", "NVDA", "^VIX", "CL=F"],
    )
    monkeypatch.setattr(
        "config_loader.get_config_value",
        lambda key, default="": "" if key == "OPTIONS_OI_WATCHLIST" else default,
    )
    from services.options_tickers import get_options_oi_watchlist, list_options_ui_tickers

    wl = get_options_oi_watchlist()
    assert wl == ["AMD", "MU", "NVDA", "SNDK"]
    ui = list_options_ui_tickers()
    assert "MU" in ui["tickers"]
    assert ui["by_ticker"]["MU"]["groups"] == ["game_5m"]


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


def test_options_card_context_monkeypatch(monkeypatch):
    from services.options_card_context import build_options_card_context, clear_options_card_context_cache

    clear_options_card_context_cache()
    monkeypatch.setattr(
        "services.polygon_options.polygon_options_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "services.polygon_options.fetch_option_expiration_dates",
        lambda t: ["2026-06-26"],
    )
    monkeypatch.setattr(
        "services.polygon_options.fetch_options_chain_snapshot",
        lambda ticker, expiration_date=None, limit=250: {
            "status": "ok",
            "underlying_price": 1050.0,
            "spot_source": "stocks_snapshot",
            "contracts": [
                {"contract_type": "put", "strike": 1000.0, "open_interest": 8000, "volume": 500},
                {"contract_type": "put", "strike": 1050.0, "open_interest": 2000, "volume": 100},
                {"contract_type": "call", "strike": 1100.0, "open_interest": 3000, "volume": 80},
                {"contract_type": "call", "strike": 1180.0, "open_interest": 5000, "volume": 120},
            ],
        },
    )

    r = build_options_card_context("MU", expiration_date="2026-06-26")
    assert r["status"] == "ok"
    assert r["source"] == "polygon"
    assert r["data_as_of"] == "live"
    assert r["sentiment_label"] in ("BULLISH", "BEARISH", "NEUTRAL")
    assert r["support_plate_strikes"][0] == 1000.0
    assert r["resistance_ceiling_strikes"][0] == 1180.0
    assert "dist_spot_max_pain_pct" in r
    assert "dist_spot_call_ceiling_pct" in r
    assert r["structure_gate_hint"] in ("neutral", "would_downgrade", "would_support")
    assert r["gate_hint"] in ("neutral", "would_downgrade", "would_signal")
    assert r["one_liner_ru"]


def test_options_structure_gate_helpers():
    from services.options_card_context import _compute_structure_fields, _structure_gate_hint

    structure = _compute_structure_fields(
        1100.0,
        max_pain_strike=1060.0,
        support=[{"strike": 1000.0, "oi": 8000}],
        resistance=[{"strike": 1105.0, "oi": 5000}],
    )
    assert structure["dist_spot_max_pain_pct"] == pytest.approx(3.636, rel=1e-2)
    assert structure["dist_spot_call_ceiling_pct"] == pytest.approx(0.455, rel=1e-2)
    hint, trigger = _structure_gate_hint(structure)
    assert hint == "would_downgrade"
    assert trigger == "call_ceiling_chase"


def test_options_card_context_polygon_unavailable():
    from unittest.mock import patch

    from services.options_card_context import build_options_card_context, clear_options_card_context_cache

    clear_options_card_context_cache()
    with patch("services.polygon_options.polygon_options_available", return_value=False):
        r = build_options_card_context("MU")
    assert r["status"] == "error"
    assert r["gate_hint"] == "unavailable"


def test_options_card_context_formatters():
    from services.options_card_context import (
        attach_options_polygon_to_brief,
        format_gate_hint_ru,
        format_options_card_context_html_block,
        format_options_card_context_lines_ru,
    )

    opts = {
        "status": "ok",
        "ticker": "MU",
        "sentiment_label": "BEARISH",
        "sentiment_score": -0.42,
        "pcr_volume": 1.2,
        "max_pain_strike": 1050.0,
        "gate_hint": "would_downgrade",
        "one_liner_ru": "Spot $1 050 · рынок — ожидание снижения.",
        "data_as_of": "live",
        "expiration_date": "2026-06-26",
    }
    lines = format_options_card_context_lines_ru(opts)
    assert any("BEARISH" in ln for ln in lines)
    assert format_gate_hint_ru("would_downgrade").startswith("shadow")
    html = format_options_card_context_html_block(opts)
    assert "Options (Polygon)" in html
    assert "/options/map" in html

    brief: dict = {"status": "ok", "symbol": "MU"}
    attach_options_polygon_to_brief(brief, symbol="MU", event_date=None)
    assert "options_polygon" in brief


def test_snapshot_options_chain_oi_yfinance_dry_run(monkeypatch):
    from scripts import snapshot_options_chain_oi as snap

    def fake_exps(ticker: str):
        assert ticker == "MU"
        return ["2026-06-26"]

    def fake_chain(ticker: str, *, expiration_date: str):
        assert expiration_date == "2026-06-26"
        return {
            "status": "ok",
            "underlying_price": 1200.0,
            "contracts": [
                {"strike": 1200.0, "contract_type": "put", "open_interest": 100, "volume": 10},
                {"strike": 1250.0, "contract_type": "call", "open_interest": 0, "volume": 0},
            ],
        }

    monkeypatch.setattr("services.yfinance_options.fetch_yfinance_option_expirations", fake_exps)
    monkeypatch.setattr("services.yfinance_options.fetch_yfinance_option_chain", fake_chain)

    r = snap.snapshot_ticker("MU", expiration_date=None, dry_run=True)
    assert r["status"] == "ok"
    assert r["source"] == "yfinance"
    assert r["rows"] == 1

# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from scripts.ingest_market_regime_daily import _build_rows
from services.macro_premarket_risk import evaluate_macro_premarket_risk
from services.ticker_groups import (
    get_macro_equity_index_tickers,
    get_market_index_tickers,
    get_market_ndx_ticker,
)


def test_market_index_tickers_defaults_include_ndx():
    assert "^NDX" in get_market_index_tickers()
    assert get_market_ndx_ticker() == "^NDX"
    assert "^NDX" in get_macro_equity_index_tickers()
    assert "SPY" in get_macro_equity_index_tickers()


def test_build_rows_prefers_ndx_over_qqq_fallback():
    closes = pd.DataFrame(
        [
            {"ticker": "SPY", "trade_date": pd.Timestamp("2026-06-01").date(), "close": 500.0},
            {"ticker": "^NDX", "trade_date": pd.Timestamp("2026-06-01").date(), "close": 18000.0},
            {"ticker": "QQQ", "trade_date": pd.Timestamp("2026-06-01").date(), "close": 450.0},
            {"ticker": "DIA", "trade_date": pd.Timestamp("2026-06-01").date(), "close": 390.0},
            {"ticker": "^VIX", "trade_date": pd.Timestamp("2026-06-01").date(), "close": 14.0},
        ]
    )
    rows = _build_rows(closes, [pd.Timestamp("2026-06-01").date()])
    assert len(rows) == 1
    assert rows[0]["ndx_close"] == 18000.0
    feats = __import__("json").loads(rows[0]["features_json"])
    assert feats["ndx_ticker"] == "^NDX"


def test_macro_risk_collects_equity_index_gaps():
    def fake_gap(ticker: str):
        if ticker == "^NDX":
            return {"gap_pct": 0.42, "source": "quotes_2d", "premarket_last": 18100.0, "prev_close": 18020.0}
        if ticker == "SPY":
            return {"gap_pct": 0.15, "source": "quotes_2d", "premarket_last": 501.0, "prev_close": 500.0}
        return {"gap_pct": None, "source": "none", "premarket_last": None, "prev_close": None}

    with patch("services.macro_premarket_risk.macro_risk_enabled", return_value=True), patch(
        "services.macro_premarket_risk.get_indicator_gap_detail", side_effect=fake_gap
    ):
        macro = evaluate_macro_premarket_risk()
    assert macro["indicators"]["^NDX"]["gap_pct"] == 0.42
    assert macro["indicators"]["SPY"]["gap_pct"] == 0.15

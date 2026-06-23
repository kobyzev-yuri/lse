# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest.mock import patch

from services.portfolio_card import get_portfolio_trade_tickers
from services.ticker_groups import (
    DEFAULT_PORTFOLIO_COMMODITY_INDICATORS,
    DEFAULT_TICKERS_INDICATOR_ONLY,
    get_tickers_for_portfolio_game,
    get_tickers_indicator_only,
)


def test_default_indicator_only_includes_vix_and_commodities():
    with patch("services.ticker_groups.get_config_value", return_value=""):
        ind = get_tickers_indicator_only()
    assert "^VIX" in ind
    for c in DEFAULT_PORTFOLIO_COMMODITY_INDICATORS:
        assert c in ind
    assert DEFAULT_TICKERS_INDICATOR_ONLY.count(",") == 3


def test_commodities_excluded_from_portfolio_trades_by_default():
    with patch("services.ticker_groups.get_config_value") as m:

        def _cfg(key, default=""):
            if key == "TICKERS_INDICATOR_ONLY":
                return ""
            if key == "TRADING_CYCLE_TICKERS":
                return ""
            if key == "TICKERS_MEDIUM":
                return "AMD,ORCL"
            if key == "TICKERS_LONG":
                return "MSFT,GC=F,CL=F,BZ=F,^VIX"
            return default

        m.side_effect = _cfg
        trade = get_portfolio_trade_tickers()
        full = get_tickers_for_portfolio_game()
    assert "MSFT" in trade and "AMD" in trade
    for c in DEFAULT_PORTFOLIO_COMMODITY_INDICATORS:
        assert c in full
        assert c not in trade
    assert "^VIX" not in trade


def test_explicit_indicator_only_env_overrides_default():
    with patch("services.ticker_groups.get_config_value", return_value="^VIX"):
        assert get_tickers_indicator_only() == ["^VIX"]

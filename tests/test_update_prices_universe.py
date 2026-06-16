"""Tests for default price ticker union."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from update_prices import get_default_price_tickers


@patch("update_prices.get_tracked_tickers", return_value=["MSFT"])
@patch("update_prices.get_tickers_from_config", return_value=["AAPL"])
@patch("services.earnings_intelligence_universe.get_earnings_intelligence_universe", return_value=["NVDA", "AAPL"])
def test_get_default_price_tickers_merges_universe(_eu, _cfg, _db):
    engine = MagicMock()
    tickers = get_default_price_tickers(engine)
    assert tickers == ["AAPL", "NVDA", "MSFT"]

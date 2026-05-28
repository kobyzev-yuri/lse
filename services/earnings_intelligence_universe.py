"""Ticker universe for earnings intelligence (GAME_5M + portfolio + spillover context)."""
from __future__ import annotations

from services.ticker_groups import (
    get_config_ticker_symbols_upper_unique,
    get_game_5m_correlation_context,
    get_tickers_for_5m_correlation,
    get_tickers_for_portfolio_game,
    get_tickers_game_5m,
)

# Always include major spillover names even if absent from config.env universe.
_EXTRA_EQUITY_SYMBOLS: tuple[str, ...] = (
    "NVDA",
    "GOOGL",
    "AVGO",
    "ANET",
    "DELL",
    "PLTR",
    "ARM",
    "INTC",
)

# ETFs / macro / forex — useful for correlation, not earnings material ingest.
_NON_EQUITY_SYMBOLS: frozenset[str] = frozenset(
    {
        "SMH",
        "SOXX",
        "QQQ",
        "TLT",
        "SPY",
        "NDX",
        "DIA",
        "VIX",
        "^VIX",
        "CL=F",
        "BZ=F",
        "GC=F",
        "GBPUSD=X",
        "US_MACRO",
        "MACRO",
    }
)


def is_equity_symbol(symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    if not sym or sym in _NON_EQUITY_SYMBOLS:
        return False
    if sym.startswith("^"):
        return False
    if sym.endswith("=F") or sym.endswith("=X") or "=X" in sym:
        return False
    return True


def get_earnings_intelligence_universe(*, include_correlation_context: bool = True) -> list[str]:
    """
    Equities for earnings materials sync / extract / brief.

    Union of:
    - GAME_5M tickers
    - portfolio (medium + long / TRADING_CYCLE_TICKERS)
    - config FAST+MEDIUM+LONG universe
    - optional GAME_5M correlation context (NVDA, GOOGL, …)
    - explicit spillover extras (NVDA, GOOGL, …)
    """
    candidates: list[str] = []
    candidates.extend(get_tickers_game_5m())
    candidates.extend(get_tickers_for_portfolio_game())
    candidates.extend(get_config_ticker_symbols_upper_unique())
    if include_correlation_context:
        candidates.extend(get_game_5m_correlation_context())
        candidates.extend(get_tickers_for_5m_correlation())
    candidates.extend(_EXTRA_EQUITY_SYMBOLS)

    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        sym = str(raw).strip().upper()
        if not is_equity_symbol(sym) or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return sorted(out)


def universe_symbols_csv(*, include_correlation_context: bool = True) -> str:
    return ",".join(get_earnings_intelligence_universe(include_correlation_context=include_correlation_context))

"""Ticker universe for earnings intelligence (GAME_5M + portfolio + spillover context)."""
from __future__ import annotations

from config_loader import get_config_value
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
    "TSM",
    "GOOGL",
    "AVGO",
    "ANET",
    "DELL",
    "PLTR",
    "ARM",
    "INTC",
)

# Default yfinance earnings calendar when EARNINGS_TRACK_TICKERS / YFINANCE_EARNINGS_TICKERS unset.
# GAME_5M names + megacap / semi market movers (NVDA, TSM, …).
DEFAULT_EARNINGS_TRACK_TICKERS: str = (
    "SNDK,NBIS,ASML,MU,LITE,CIEN,ALAB,TER,"
    "NVDA,AMD,AVGO,INTC,ARM,ANET,DELL,TSM,"
    "MSFT,META,GOOGL,AMZN,ORCL,PLTR"
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
        "^NDX",
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


def _parse_ticker_csv(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def get_earnings_calendar_tickers() -> list[str]:
    """
    Tickers for Yahoo/yfinance earnings KB seeding (and NewsAPI equity fallback).

    Union of:
    - YFINANCE_EARNINGS_TICKERS or EARNINGS_TRACK_TICKERS from config (if set)
    - else DEFAULT_EARNINGS_TRACK_TICKERS
    - always: equities from get_earnings_intelligence_universe (NVDA, DELL, … even when config is slim)
    """
    yf_raw = (get_config_value("YFINANCE_EARNINGS_TICKERS", "") or "").strip()
    if yf_raw:
        explicit = _parse_ticker_csv(yf_raw)
    else:
        track_raw = (get_config_value("EARNINGS_TRACK_TICKERS", "") or "").strip()
        explicit = _parse_ticker_csv(track_raw) if track_raw else _parse_ticker_csv(DEFAULT_EARNINGS_TRACK_TICKERS)

    candidates = list(explicit) + list(get_earnings_intelligence_universe(include_correlation_context=False))
    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        sym = str(raw).strip().upper()
        if not is_equity_symbol(sym) or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return sorted(out)


def get_event_reaction_symbol_allowlist() -> list[str]:
    """
    Symbols for event_reaction_dataset skeleton / config-scoped backfill.

    Union of TICKERS_FAST+MEDIUM+LONG and earnings intelligence equities so spillover
    names (ANET, GOOGL, …) stay in ERD even when omitted from a slim config.env.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in list(get_config_ticker_symbols_upper_unique()) + list(
        get_earnings_intelligence_universe(include_correlation_context=False)
    ):
        sym = str(raw).strip().upper()
        if not sym or sym in seen or not is_equity_symbol(sym):
            continue
        seen.add(sym)
        out.append(sym)
    return sorted(out)

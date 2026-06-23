"""
Option chain через Yahoo Finance (yfinance) — для сравнения с Polygon snapshot.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def fetch_yfinance_option_expirations(ticker: str) -> List[str]:
    """Список дат экспирации из Yahoo (Ticker.options)."""
    import yfinance as yf

    sym = (ticker or "").strip().upper()
    if not sym:
        return []
    try:
        exps = list(getattr(yf.Ticker(sym), "options", []) or [])
        return sorted(str(d) for d in exps)
    except Exception as e:
        logger.warning("yfinance expirations %s: %s", sym, e)
        return []


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        import pandas as pd

        if pd.isna(v):
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int:
    f = _safe_float(v)
    if f is None:
        return 0
    return int(f)


def _rows_to_contracts(
    rows: Any,
    *,
    contract_type: str,
    expiration_date: str,
    underlying: str,
    underlying_price: Optional[float],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if rows is None or len(rows) == 0:
        return out
    for _, row in rows.iterrows():
        strike = _safe_float(row.get("strike"))
        if strike is None:
            continue
        bid = _safe_float(row.get("bid"))
        ask = _safe_float(row.get("ask"))
        last = _safe_float(row.get("lastPrice"))
        out.append(
            {
                "ticker": str(row.get("contractSymbol") or ""),
                "underlying": underlying,
                "underlying_price": underlying_price,
                "expiration_date": expiration_date,
                "strike": strike,
                "contract_type": contract_type,
                "volume": _safe_int(row.get("volume")),
                "open_interest": _safe_int(row.get("openInterest")),
                "bid": bid,
                "ask": ask,
                "last": last,
                "implied_volatility": _safe_float(row.get("impliedVolatility")),
                "delta": None,
            }
        )
    return out


def fetch_yfinance_option_chain(
    ticker: str,
    *,
    expiration_date: str,
) -> Dict[str, Any]:
    """Цепочка call+put на одну экспирацию в формате, совместимом с polygon_options."""
    import yfinance as yf

    from services.options_calculator_prefill import fetch_spot_yfinance

    sym = (ticker or "").strip().upper()
    exp = (expiration_date or "").strip()
    if not sym or not exp:
        return {"status": "error", "error": "ticker and expiration_date required", "contracts": []}

    spot_payload = fetch_spot_yfinance(sym)
    spot = spot_payload.get("spot") if spot_payload.get("status") == "ok" else None
    dropped = 0

    try:
        t = yf.Ticker(sym)
        chain = t.option_chain(exp)
        calls = _rows_to_contracts(
            chain.calls,
            contract_type="call",
            expiration_date=exp,
            underlying=sym,
            underlying_price=spot,
        )
        puts = _rows_to_contracts(
            chain.puts,
            contract_type="put",
            expiration_date=exp,
            underlying=sym,
            underlying_price=spot,
        )
        contracts = calls + puts
        # Убираем «мёртвые» строки без OI и volume — шум Yahoo
        liquid = [c for c in contracts if c["volume"] > 0 or c["open_interest"] > 0]
        dropped = len(contracts) - len(liquid)
        contracts = liquid
    except Exception as e:
        logger.warning("yfinance chain %s %s: %s", sym, exp, e)
        return {"status": "error", "error": str(e), "contracts": [], "underlying": sym}

    if not contracts:
        return {
            "status": "empty",
            "underlying": sym,
            "underlying_price": spot,
            "expiration_date": exp,
            "contract_count": 0,
            "contracts": [],
            "source": "yfinance",
        }

    return {
        "status": "ok",
        "underlying": sym,
        "underlying_price": spot,
        "expiration_date": exp,
        "contract_count": len(contracts),
        "contracts": contracts,
        "source": "yfinance",
        "calls_count": len(calls),
        "puts_count": len(puts),
        "spot_source": spot_payload.get("price_kind") if spot_payload.get("status") == "ok" else None,
        "dropped_zero_oi_volume": dropped,
    }

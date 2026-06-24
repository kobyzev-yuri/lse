"""
Подстановка полей калькулятора опционов: spot (yfinance) и даты earnings (knowledge_base).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def _mid_option_price(
    bid: Optional[float], ask: Optional[float], last: Optional[float]
) -> Optional[float]:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    if last is not None and float(last) > 0:
        return float(last)
    if bid is not None and float(bid) > 0:
        return float(bid)
    if ask is not None and float(ask) > 0:
        return float(ask)
    return None


def round_option_strike(strike: float) -> float:
    """Округление к типичному шагу страйка US equity options."""
    s = float(strike)
    if s >= 500:
        step = 10.0
    elif s >= 200:
        step = 5.0
    elif s >= 25:
        step = 1.0
    else:
        step = 0.5
    return round(s / step) * step


def fetch_spot_yfinance(ticker: str) -> Dict[str, Any]:
    """Текущая / последняя цена акции только через yfinance (без fallback)."""
    import yfinance as yf

    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym}

    t = yf.Ticker(sym)
    as_of: Optional[str] = None
    price: Optional[float] = None
    price_kind = "unknown"

    # 1) Yahoo quote summary — стабильнее для spot, чем случайная минутная свеча
    try:
        info = getattr(t, "info", None) or {}
        for key in ("regularMarketPrice", "currentPrice", "postMarketPrice", "preMarketPrice"):
            p = info.get(key)
            if p is not None and float(p) > 0:
                price = float(p)
                price_kind = f"info.{key}"
                break
    except Exception as e:
        logger.debug("yfinance info spot %s: %s", sym, e)

    if price is None:
        try:
            df = t.history(period="1d", interval="1m", auto_adjust=False)
            if df is not None and not df.empty and "Close" in df.columns:
                price = float(df["Close"].iloc[-1])
                ts = df.index[-1]
                as_of = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                price_kind = "intraday_last"
        except Exception as e:
            logger.debug("yfinance intraday %s: %s", sym, e)

    if price is None:
        try:
            df = t.history(period="5d", interval="1d", auto_adjust=False)
            if df is not None and not df.empty and "Close" in df.columns:
                price = float(df["Close"].iloc[-1])
                ts = df.index[-1]
                as_of = ts.date().isoformat() if hasattr(ts, "date") else str(ts)
                price_kind = "daily_close"
        except Exception as e:
            logger.warning("yfinance spot %s: %s", sym, e)
            return {"status": "error", "error": str(e), "ticker": sym}

    if price is None or price <= 0:
        return {"status": "error", "error": f"нет котировки yfinance для {sym}", "ticker": sym}

    return {
        "status": "ok",
        "ticker": sym,
        "spot": round(price, 4),
        "source": "yfinance",
        "price_kind": price_kind,
        "as_of": as_of,
    }


def load_ticker_earnings_calendar(engine: Engine, ticker: str) -> Dict[str, Any]:
    """
    Даты earnings из knowledge_base (+ метаданные earnings_event_detail).
    Предлагает ближайшую будущую дату; экспирацию — первая из Polygon reference >= earnings.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym}

    q = text(
        """
        SELECT
          kb.id AS knowledge_base_id,
          kb.ts::date AS event_date,
          kb.source,
          ed.guidance_summary->>'report_timing' AS report_timing
        FROM knowledge_base kb
        LEFT JOIN earnings_event_detail ed ON ed.knowledge_base_id = kb.id
        WHERE UPPER(TRIM(kb.ticker)) = :ticker
          AND UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
        ORDER BY kb.ts::date ASC, kb.id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, {"ticker": sym}).mappings().all()

    events: List[Dict[str, Any]] = []
    for r in rows:
        ev_d = r.get("event_date")
        if ev_d is None:
            continue
        if isinstance(ev_d, datetime):
            ev_d = ev_d.date()
        events.append(
            {
                "knowledge_base_id": r.get("knowledge_base_id"),
                "event_date": ev_d.isoformat(),
                "source": r.get("source"),
                "report_timing": r.get("report_timing"),
            }
        )

    if not events:
        return {
            "status": "error",
            "error": f"в knowledge_base нет EARNINGS для {sym}",
            "ticker": sym,
            "hint_ru": "Запустите ingest earnings (yfinance → KB) или добавьте событие в календарь.",
        }

    today = date.today()
    future = [e for e in events if date.fromisoformat(e["event_date"]) >= today]
    if future:
        picked = future[0]
        pick_reason = "nearest_future"
    else:
        picked = events[-1]
        pick_reason = "last_known_past"

    suggested_earnings = picked["event_date"]
    suggested_expiration, exp_source = _suggest_expiration(sym, suggested_earnings)

    return {
        "status": "ok",
        "ticker": sym,
        "source": "knowledge_base",
        "events": events,
        "suggested_earnings_date": suggested_earnings,
        "suggested_expiration_date": suggested_expiration,
        "expiration_source": exp_source,
        "pick_reason": pick_reason,
    }


def _suggest_expiration(ticker: str, earnings_date: str) -> tuple[Optional[str], Optional[str]]:
    """Первая дата экспирации из Polygon reference API на/после earnings."""
    try:
        from services.polygon_options import fetch_option_expiration_dates, polygon_options_available

        if not polygon_options_available():
            return None, None
        exps = fetch_option_expiration_dates(ticker)
        for exp in exps:
            if exp >= earnings_date:
                return exp, "polygon_reference"
    except Exception as e:
        logger.debug("expiration suggest %s: %s", ticker, e)
    return None, None


def _select_puts_for_prefill(
    puts: List[Dict[str, Any]],
    spot: float,
    strategy: str,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """ATM long put и optional short для spread (~5% ниже spot)."""
    if not puts:
        raise ValueError("нет put в цепочке")
    if strategy == "put_spread":
        on_or_above = [p for p in puts if float(p["strike"]) >= spot * 0.995]
        long_put = min(on_or_above or puts, key=lambda c: abs(float(c["strike"]) - spot))
    else:
        long_put = min(puts, key=lambda c: abs(float(c["strike"]) - spot))
    short_put: Optional[Dict[str, Any]] = None
    if strategy == "put_spread":
        target_short = spot * 0.95
        long_strike = float(long_put["strike"])
        candidates = [p for p in puts if float(p["strike"]) < long_strike - 0.01]
        if candidates:
            short_put = min(candidates, key=lambda c: abs(float(c["strike"]) - target_short))
    return long_put, short_put


def _build_prefill_from_puts(
    *,
    ticker: str,
    spot: float,
    spot_source: Optional[str],
    expiration_date: str,
    available_expirations: List[str],
    strategy: str,
    puts: List[Dict[str, Any]],
    source: str,
) -> Dict[str, Any]:
    """Общая сборка prefill из списка put-контрактов (Polygon или yfinance)."""
    sym = ticker.strip().upper()
    liquid = [
        p
        for p in puts
        if int(p.get("volume") or 0) > 0 or int(p.get("open_interest") or 0) > 0
    ]
    if not liquid:
        return {
            "status": "error",
            "error": f"нет ликвидных put на экспирацию {expiration_date}",
            "ticker": sym,
            "spot": round(spot, 4),
            "expiration_date": expiration_date,
            "source": source,
        }

    try:
        long_put, short_put = _select_puts_for_prefill(liquid, spot, strategy)
    except ValueError as e:
        return {
            "status": "error",
            "error": str(e),
            "ticker": sym,
            "spot": round(spot, 4),
            "expiration_date": expiration_date,
            "source": source,
        }

    long_strike = float(long_put["strike"])
    long_prem = _mid_option_price(long_put.get("bid"), long_put.get("ask"), long_put.get("last"))
    if long_prem is None or long_prem <= 0:
        return {
            "status": "error",
            "error": f"нет bid/ask/last у put {long_strike}",
            "ticker": sym,
            "spot": round(spot, 4),
            "expiration_date": expiration_date,
            "source": source,
            "long_strike": long_strike,
        }

    out: Dict[str, Any] = {
        "status": "ok",
        "ticker": sym,
        "spot": round(spot, 4),
        "spot_source": spot_source,
        "expiration_date": expiration_date,
        "available_expirations": available_expirations,
        "strategy": strategy,
        "long_strike": long_strike,
        "long_premium": round(long_prem, 2),
        "source": source,
        "price_kind": _price_kind_label(long_put),
    }

    if strategy == "put_spread":
        if short_put is None:
            out["status"] = "partial"
            out["warning_ru"] = "Не найден short put ниже long — заполнен только long."
            return out
        short_prem = _mid_option_price(
            short_put.get("bid"), short_put.get("ask"), short_put.get("last")
        )
        if short_prem is None or short_prem <= 0:
            out["status"] = "partial"
            out["warning_ru"] = "Short put без котировки — заполнен только long."
            return out
        out["short_strike"] = float(short_put["strike"])
        out["short_premium"] = round(short_prem, 2)

    return out


def _price_kind_label(contract: Dict[str, Any]) -> str:
    bid, ask, last = contract.get("bid"), contract.get("ask"), contract.get("last")
    if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
        return "mid(bid/ask)"
    if last is not None and float(last) > 0:
        return "last"
    if bid is not None and float(bid) > 0:
        return "bid"
    if ask is not None and float(ask) > 0:
        return "ask"
    return "unknown"


def fetch_calculator_polygon_prefill(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    strategy: str = "pure_put",
) -> Dict[str, Any]:
    """Spot + ATM put (+ spread) из Polygon options snapshot."""
    from services.polygon_options import (
        fetch_option_expiration_dates,
        fetch_options_chain_snapshot,
        polygon_options_available,
    )

    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym}
    if not polygon_options_available():
        return {"status": "error", "error": "POLYGON_API_KEY не настроен", "ticker": sym}

    exps = fetch_option_expiration_dates(sym)
    exp = (expiration_date or "").strip() or (exps[0] if exps else "")
    if not exp:
        return {"status": "error", "error": f"Polygon: нет дат экспирации для {sym}", "ticker": sym}

    chain = fetch_options_chain_snapshot(sym, expiration_date=exp)
    if chain.get("status") == "error":
        return {
            "status": "error",
            "error": chain.get("error") or "ошибка Polygon snapshot",
            "ticker": sym,
            "expiration_date": exp,
        }
    spot = chain.get("underlying_price")
    if spot is None or float(spot) <= 0:
        return {
            "status": "error",
            "error": "Polygon: spot недоступен",
            "ticker": sym,
            "expiration_date": exp,
        }

    puts = [c for c in chain.get("contracts") or [] if c.get("contract_type") == "put"]
    return _build_prefill_from_puts(
        ticker=sym,
        spot=float(spot),
        spot_source=chain.get("spot_source"),
        expiration_date=exp,
        available_expirations=exps,
        strategy=strategy,
        puts=puts,
        source="polygon",
    )


def fetch_calculator_yfinance_prefill(
    ticker: str,
    *,
    expiration_date: Optional[str] = None,
    strategy: str = "pure_put",
) -> Dict[str, Any]:
    """
    Spot + ATM put (и опционально нога spread) из yfinance option_chain.
    Премия — mid(bid, ask) или last; без Polygon.
    """
    from services.yfinance_options import (
        fetch_yfinance_option_chain,
        fetch_yfinance_option_expirations,
    )

    sym = (ticker or "").strip().upper()
    if not sym:
        return {"status": "error", "error": "ticker required", "ticker": sym}

    spot_payload = fetch_spot_yfinance(sym)
    if spot_payload.get("status") != "ok":
        return spot_payload

    spot = float(spot_payload["spot"])
    exps = fetch_yfinance_option_expirations(sym)
    exp = (expiration_date or "").strip() or (exps[0] if exps else "")
    if not exp:
        return {
            "status": "error",
            "error": f"yfinance: нет дат экспирации для {sym}",
            "ticker": sym,
            "spot": spot,
        }

    chain = fetch_yfinance_option_chain(sym, expiration_date=exp)
    if chain.get("status") != "ok":
        return {
            "status": "error",
            "error": chain.get("error") or "пустая цепочка yfinance",
            "ticker": sym,
            "spot": spot,
            "expiration_date": exp,
        }

    puts = [c for c in chain.get("contracts") or [] if c.get("contract_type") == "put"]
    return _build_prefill_from_puts(
        ticker=sym,
        spot=spot,
        spot_source=spot_payload.get("price_kind"),
        expiration_date=exp,
        available_expirations=exps,
        strategy=strategy,
        puts=puts,
        source="yfinance",
    )

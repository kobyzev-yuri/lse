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

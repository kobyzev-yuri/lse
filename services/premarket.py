"""
Контекст премаркета для решений: цена премаркета, гэп к предыдущему закрытию, минуты до открытия.
Источник: yfinance с prepost=True (1m за текущий день). Предыдущий close — из quotes или history.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    NYSE_TZ = ZoneInfo("America/New_York")
except ImportError:
    NYSE_TZ = None

NYSE_OPEN_TIME = time(9, 30)


def _et_now() -> Optional[datetime]:
    if NYSE_TZ is None:
        return None
    from datetime import timezone
    utc = datetime.now(timezone.utc)
    return utc.astimezone(NYSE_TZ)


def _minutes_until_open(et_now: Optional[datetime]) -> Optional[int]:
    """Минуты до 9:30 ET сегодня."""
    if et_now is None:
        return None
    today = et_now.date()
    open_et = datetime.combine(today, NYSE_OPEN_TIME, tzinfo=NYSE_TZ)
    if et_now >= open_et:
        return 0
    delta = open_et - et_now
    return int(delta.total_seconds() / 60)


def get_prev_close_from_db(ticker: str) -> Optional[float]:
    """Последний close из quotes (предыдущий торговый день)."""
    try:
        from config_loader import get_database_url
        from sqlalchemy import create_engine, text
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT close FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC
                    LIMIT 1
                """),
                {"ticker": ticker},
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        logger.debug("prev_close из БД для %s: %s", ticker, e)
    return None


def get_premarket_context(ticker: str, dt_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Контекст премаркета по тикеру. Вызывать только при session_phase == PRE_MARKET.

    Returns:
        - prev_close: float | None — последний close регулярной сессии (вчера)
        - premarket_last: float | None — последняя цена в премаркете
        - premarket_gap_pct: float | None — гэп % к prev_close
        - premarket_volume: int | None — объём премаркета (если есть)
        - minutes_until_open: int | None — минуты до 9:30 ET
        - error: str | None — при ошибке
    """
    out: Dict[str, Any] = {
        "prev_close": None,
        "premarket_last": None,
        "premarket_gap_pct": None,
        "premarket_volume": None,
        "minutes_until_open": None,
        "error": None,
    }
    et = _et_now() if dt_utc is None else (dt_utc.astimezone(NYSE_TZ) if NYSE_TZ and getattr(dt_utc, "tzinfo", None) else _et_now())
    out["minutes_until_open"] = _minutes_until_open(et)

    prev_close = get_prev_close_from_db(ticker)
    if prev_close is None:
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            df = t.history(period="5d", interval="1d", auto_adjust=False)
            if df is not None and not df.empty and "Close" in df.columns:
                prev_close = float(df["Close"].iloc[-1])
        except Exception as e:
            logger.debug("prev_close из yfinance для %s: %s", ticker, e)
    out["prev_close"] = prev_close

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # 1d + 1m + prepost — даёт сегодняшний день с премаркетом
        df = t.history(period="1d", interval="1m", prepost=True, auto_adjust=False)
        if df is None or df.empty:
            out["error"] = "нет данных премаркета"
            return out
        df = df.rename_axis("Datetime").reset_index()
        if "Datetime" not in df.columns and "Date" in df.columns:
            df = df.rename(columns={"Date": "Datetime"})
        if "Close" not in df.columns:
            out["error"] = "нет колонки Close"
            return out
        # Берём последнюю цену (при вызове в PRE_MARKET это текущая цена премаркета)
        premarket_last = float(df["Close"].iloc[-1])
        premarket_vol = int(df["Volume"].iloc[-1]) if "Volume" in df.columns and len(df) else None
        out["premarket_last"] = premarket_last
        out["premarket_volume"] = premarket_vol
        if prev_close is not None and prev_close > 0 and premarket_last is not None:
            out["premarket_gap_pct"] = round((premarket_last / prev_close - 1.0) * 100.0, 2)
    except Exception as e:
        logger.warning("Премаркет для %s: %s", ticker, e)
        out["error"] = str(e)

    return out

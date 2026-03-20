"""
Контекст премаркета для решений: цена премаркета, гэп к предыдущему закрытию, минуты до открытия.
Источник: yfinance с prepost=True (1m за текущий день). Предыдущий close — из quotes или history.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional

import pandas as pd

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
        - premarket_last: float | None — последняя цена в премаркете (последняя минута Yahoo)
        - premarket_last_time_et: метка времени этой минуты (для сравнения с данными «там»)
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
        # Сортируем по времени — yfinance может вернуть строки не по порядку или в UTC
        dt_col = "Datetime" if "Datetime" in df.columns else "Date"
        if dt_col in df.columns:
            df = df.sort_values(dt_col).reset_index(drop=True)
        # Берём последнюю цену (последняя минута премаркета по времени)
        last_ts = df[dt_col].iloc[-1] if dt_col in df.columns else None
        premarket_last = float(df["Close"].iloc[-1])
        premarket_vol = int(df["Volume"].iloc[-1]) if "Volume" in df.columns and len(df) else None
        out["premarket_last"] = premarket_last
        out["premarket_volume"] = premarket_vol
        # В ET для сравнения с Yahoo (приводим к ET если есть tz)
        try:
            if last_ts is not None and hasattr(last_ts, "tz_convert") and NYSE_TZ is not None:
                last_ts = last_ts.tz_convert(NYSE_TZ) if getattr(last_ts, "tzinfo", None) is not None else last_ts
        except Exception:
            pass
        out["premarket_last_time_et"] = str(last_ts) if last_ts is not None else None
        if prev_close is not None and prev_close > 0 and premarket_last is not None:
            out["premarket_gap_pct"] = round((premarket_last / prev_close - 1.0) * 100.0, 2)
    except Exception as e:
        logger.warning("Премаркет для %s: %s", ticker, e)
        out["error"] = str(e)

    return out


def get_premarket_intraday_momentum_pct(ticker: str) -> Optional[float]:
    """
    Импульс **внутри** премаркета (ET): первая vs последняя цена 1m со свечами строго до открытия RTH 9:30.
    Не путать с premarket_gap_pct (к вчерашнему close): здесь только дрейф от начала премаркет-ленты до 9:30.

    Используется в game 5m в первые минуты RTH, когда по 5m ещё мало баров текущей сессии —
    чтобы не опираться на «вчера + гэп» в полном df, а на фактическое поведение до колокола.

    Returns:
        Процент изменения или None при недостатке данных.
    """
    df = get_premarket_ohlc(ticker)
    if df is None or df.empty or "Close" not in df.columns:
        return None
    dt_col = "Datetime" if "Datetime" in df.columns else "Date"
    try:
        dts = pd.to_datetime(df[dt_col])
        if dts.dt.tz is None:
            try:
                dts = dts.dt.tz_localize("America/New_York", ambiguous=True)
            except Exception:
                dts = dts.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
        else:
            dts = dts.dt.tz_convert("America/New_York")
        et_now = _et_now()
        today = et_now.date() if et_now is not None else dts.max().date()
        t_open = NYSE_OPEN_TIME
        mask = (dts.dt.date == today) & (dts.dt.time < t_open)
        sub = df.loc[mask].copy()
        if len(sub) < 2:
            return None
        sub = sub.sort_values(dt_col).reset_index(drop=True)
        c0 = float(sub["Close"].iloc[0])
        c1 = float(sub["Close"].iloc[-1])
        if c0 <= 0:
            return None
        return round((c1 / c0 - 1.0) * 100.0, 4)
    except Exception as e:
        logger.debug("premarket intraday momentum %s: %s", ticker, e)
        return None


def get_premarket_ohlc(ticker: str):
    """
    OHLC 1m за сегодня с премаркетом (prepost) для графика.
    Returns: DataFrame с колонками Datetime, Open, High, Low, Close, Volume или None при ошибке.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.history(period="1d", interval="1m", prepost=True, auto_adjust=False)
        if df is None or df.empty or "Close" not in df.columns:
            return None
        df = df.rename_axis("Datetime").reset_index()
        if "Datetime" not in df.columns and "Date" in df.columns:
            df = df.rename(columns={"Date": "Datetime"})
        dt_col = "Datetime" if "Datetime" in df.columns else "Date"
        if dt_col in df.columns:
            df = df.sort_values(dt_col).reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning("get_premarket_ohlc %s: %s", ticker, e)
        return None

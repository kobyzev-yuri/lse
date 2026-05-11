"""
Календарь дат отчётности (earnings) через Yahoo / yfinance — без API-ключа.

Дополняет Alpha Vantage (часто недоступен на free tier). Записи в knowledge_base:
event_type=EARNINGS, source=Yahoo Finance (yfinance).

Дедуп: одна строка на (ticker, дата события) для любого source EARNINGS — см. alphavantage save_earnings_to_db.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

from config_loader import get_config_value, get_database_url

logger = logging.getLogger(__name__)

YFINANCE_EARNINGS_SOURCE = "Yahoo Finance (yfinance)"


def _is_equity_earnings_symbol(sym: str) -> bool:
    s = (sym or "").strip().upper()
    if not s or s in ("MACRO", "US_MACRO"):
        return False
    if "=" in s:
        return False
    if s.startswith("^"):
        return False
    return True


def _yfinance_earnings_ticker_list() -> List[str]:
    raw = (get_config_value("YFINANCE_EARNINGS_TICKERS", "") or "").strip()
    if raw:
        return [t.strip().upper() for t in raw.split(",") if t.strip() and _is_equity_earnings_symbol(t.strip())]
    raw2 = get_config_value("EARNINGS_TRACK_TICKERS", "MSFT,SNDK,MU,LITE,ALAB,TER") or ""
    return [t.strip().upper() for t in raw2.split(",") if t.strip() and _is_equity_earnings_symbol(t.strip())]


def _report_datetime_from_index(ts: Any) -> Optional[datetime]:
    """Индекс даты earnings → naive datetime 00:00 (дата события для KB)."""
    try:
        import pandas as pd

        dt = pd.Timestamp(ts)
        if dt.tzinfo is not None:
            dt = dt.tz_convert("UTC").tz_localize(None)
        return datetime(int(dt.year), int(dt.month), int(dt.day))
    except Exception:
        return None


def _eps_estimate_from_row(row: Any) -> Optional[float]:
    import math

    import pandas as pd

    for col in ("EPS Estimate", "Eps Estimate", "epsEstimate"):
        try:
            if hasattr(row, "index") and col in row.index:
                v = row[col]
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return None
                if pd.isna(v):
                    return None
                return float(v)
        except (TypeError, ValueError):
            return None
    return None


def fetch_yfinance_earnings_for_symbol(symbol: str, limit: int) -> List[Dict[str, Any]]:
    """Возвращает список {symbol, reportDate, estimate?, currency} для одного тикера."""
    import yfinance as yf

    sym = symbol.strip().upper()
    out: List[Dict[str, Any]] = []
    t = yf.Ticker(sym)
    df = None
    try:
        if hasattr(t, "get_earnings_dates"):
            df = t.get_earnings_dates(limit=max(1, int(limit)))
    except Exception as e:
        logger.debug("yfinance get_earnings_dates(%s): %s", sym, e)
        df = None
    if df is None or getattr(df, "empty", True):
        return out
    # yfinance может вернуть больше строк, чем limit — режем для контроля объёма KB
    try:
        if len(df) > int(limit):
            df = df.iloc[: int(limit)]
    except Exception:
        pass

    for idx, row in df.iterrows():
        rd = _report_datetime_from_index(idx)
        if rd is None:
            continue
        est = _eps_estimate_from_row(row)
        out.append(
            {
                "symbol": sym,
                "reportDate": rd,
                "estimate": est,
                "currency": "USD",
            }
        )
    return out


def _earnings_row_exists(conn, ticker: str, report_date: datetime) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1 FROM knowledge_base
            WHERE ticker = :ticker
              AND event_type = 'EARNINGS'
              AND DATE(ts) = DATE(:report_date)
            LIMIT 1
            """
        ),
        {"ticker": ticker, "report_date": report_date},
    ).fetchone()
    return row is not None


def save_yfinance_earnings_to_db(rows: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    """Сохраняет earnings из yfinance. Returns (saved, skipped, errors)."""
    if not rows:
        return 0, 0, 0

    db_url = get_database_url()
    engine = create_engine(db_url)
    saved = skipped = errors = 0

    try:
        from services.ticker_groups import get_tracked_tickers_for_kb, kb_ingest_tracked_tickers_only

        tracked = set(get_tracked_tickers_for_kb()) if kb_ingest_tracked_tickers_only() else None
    except Exception:
        tracked = None

    with engine.begin() as conn:
        for er in rows:
            try:
                sym = er.get("symbol") or ""
                rd = er.get("reportDate")
                if not sym or not rd:
                    skipped += 1
                    continue
                if tracked is not None and sym not in tracked:
                    skipped += 1
                    continue
                if _earnings_row_exists(conn, sym, rd):
                    skipped += 1
                    continue

                content = f"Earnings date (Yahoo/yfinance) for {sym}"
                if er.get("estimate") is not None:
                    content += f"\nEPS estimate: {er['estimate']} {er.get('currency', 'USD')}"

                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base
                        (ts, ticker, source, content, event_type, importance)
                        VALUES (:ts, :ticker, :source, :content, :event_type, :importance)
                        """
                    ),
                    {
                        "ts": rd,
                        "ticker": sym,
                        "source": YFINANCE_EARNINGS_SOURCE,
                        "content": content,
                        "event_type": "EARNINGS",
                        "importance": "HIGH",
                    },
                )
                saved += 1
            except Exception as e:
                errors += 1
                logger.warning("yfinance earnings: ошибка сохранения %s: %s", er, e)

    engine.dispose()
    return saved, skipped, errors


def fetch_and_save_yfinance_earnings() -> int:
    """
    Тянет даты отчётов по списку тикеров и пишет в KB.
    Возвращает число новых вставленных строк.
    """
    raw = (get_config_value("YFINANCE_EARNINGS_CALENDAR_SAVE", "true") or "true").strip().lower()
    if raw not in ("1", "true", "yes"):
        logger.info("📅 Yahoo earnings (yfinance): пропуск (YFINANCE_EARNINGS_CALENDAR_SAVE не true)")
        return 0

    try:
        limit = int((get_config_value("YFINANCE_EARNINGS_LIMIT", "12") or "12").strip())
    except (TypeError, ValueError):
        limit = 12
    limit = max(1, min(limit, 40))

    try:
        delay = float((get_config_value("YFINANCE_EARNINGS_DELAY_SEC", "1.0") or "1.0").strip())
    except (TypeError, ValueError):
        delay = 1.0
    delay = max(0.2, min(delay, 10.0))

    tickers = _yfinance_earnings_ticker_list()
    if not tickers:
        logger.warning("📅 Yahoo earnings (yfinance): список тикеров пуст (YFINANCE_EARNINGS_TICKERS / EARNINGS_TRACK_TICKERS)")
        return 0

    all_rows: List[Dict[str, Any]] = []
    for i, sym in enumerate(tickers):
        try:
            part = fetch_yfinance_earnings_for_symbol(sym, limit)
            all_rows.extend(part)
            logger.info("📅 yfinance earnings: %s — %s строк(и) из API", sym, len(part))
        except Exception as e:
            logger.warning("📅 yfinance earnings: %s — ошибка: %s", sym, e)
        if i < len(tickers) - 1:
            time.sleep(delay)

    saved, skipped, errors = save_yfinance_earnings_to_db(all_rows)
    logger.info(
        "✅ Yahoo earnings (yfinance): сохранено %s, пропусков %s, ошибок %s (всего событий из API: %s)",
        saved,
        skipped,
        errors,
        len(all_rows),
    )
    return saved


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    n = fetch_and_save_yfinance_earnings()
    print("saved_new:", n)

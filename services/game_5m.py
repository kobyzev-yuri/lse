"""
Игра по 5m сигналам: симуляция входов/выходов для любой быстрой бумаги.

Использует trade_history с strategy_name='GAME_5M' и ticker (SNDK, NDK, LITE, NBIS и т.д.).
Тикер передаётся явно, без привязки к одному инструменту.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import create_engine, text

from config_loader import get_database_url

logger = logging.getLogger(__name__)

GAME_5M_STRATEGY = "GAME_5M"
GAME_NOTIONAL_USD = 10_000.0
COMMISSION_RATE = 0.001
MAX_POSITION_DAYS = 2


def _engine():
    return create_engine(get_database_url())


def get_open_position(ticker: str) -> Optional[dict[str, Any]]:
    """
    Есть ли открытая позиция по тикеру в игре (GAME_5M).
    """
    engine = _engine()
    with engine.connect() as conn:
        last_buy = conn.execute(
            text("""
                SELECT id, ts, quantity, price, signal_type
                FROM public.trade_history
                WHERE ticker = :ticker AND strategy_name = :strategy AND side = 'BUY'
                ORDER BY ts DESC
                LIMIT 1
            """),
            {"ticker": ticker, "strategy": GAME_5M_STRATEGY},
        ).fetchone()
        if not last_buy:
            return None
        buy_id, buy_ts, qty, price, signal_type = last_buy
        sell_after = conn.execute(
            text("""
                SELECT 1 FROM public.trade_history
                WHERE ticker = :ticker AND strategy_name = :strategy AND side = 'SELL' AND ts > :after_ts
                LIMIT 1
            """),
            {"ticker": ticker, "strategy": GAME_5M_STRATEGY, "after_ts": buy_ts},
        ).fetchone()
        if sell_after:
            return None
    return {
        "id": buy_id,
        "entry_ts": buy_ts,
        "entry_price": float(price),
        "quantity": float(qty),
        "entry_signal_type": signal_type,
    }


def record_entry(
    ticker: str,
    price: float,
    signal_type: str,
    reasoning: Optional[str] = None,
) -> Optional[int]:
    """Фиксирует бумажный вход: INSERT BUY в trade_history (strategy_name=GAME_5M)."""
    if price <= 0:
        logger.warning("game_5m: record_entry %s с ценой <= 0, пропуск", ticker)
        return None
    if get_open_position(ticker) is not None:
        logger.info("game_5m: по %s уже есть открытая позиция, повторный вход не создаём", ticker)
        return None

    quantity = max(1, int(GAME_NOTIONAL_USD / price))
    notional = quantity * price
    commission = notional * COMMISSION_RATE
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO public.trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name)
                VALUES (CURRENT_TIMESTAMP, :ticker, 'BUY', :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy)
            """),
            {
                "ticker": ticker,
                "qty": quantity,
                "price": price,
                "commission": commission,
                "signal_type": signal_type,
                "total_value": notional,
                "strategy": GAME_5M_STRATEGY,
            },
        )
        row = conn.execute(text("SELECT LASTVAL()")).fetchone()
        new_id = row[0] if row else None
    logger.info("game_5m: вход %s id=%s @ %.2f qty=%s %s", ticker, new_id, price, quantity, signal_type)
    return new_id


def close_position(ticker: str, exit_price: float, exit_signal_type: str) -> Optional[float]:
    """Закрывает открытую позицию: INSERT SELL. Возвращает PnL в %."""
    pos = get_open_position(ticker)
    if not pos:
        logger.info("game_5m: по %s нет открытой позиции для закрытия", ticker)
        return None

    entry_price = pos["entry_price"]
    quantity = pos["quantity"]
    if entry_price <= 0 or exit_price <= 0:
        return None

    notional = quantity * exit_price
    commission = notional * COMMISSION_RATE
    log_return = math.log(exit_price / entry_price)
    pnl_pct = float(log_return * 100.0 - 2 * COMMISSION_RATE * 100.0)

    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO public.trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name)
                VALUES (CURRENT_TIMESTAMP, :ticker, 'SELL', :qty, :price, :commission, :signal_type, :total_value, NULL, :strategy)
            """),
            {
                "ticker": ticker,
                "qty": quantity,
                "price": exit_price,
                "commission": commission,
                "signal_type": exit_signal_type,
                "total_value": notional,
                "strategy": GAME_5M_STRATEGY,
            },
        )
    logger.info("game_5m: %s закрыта @ %.2f %s, PnL=%.2f%%", ticker, exit_price, exit_signal_type, pnl_pct)
    return pnl_pct


def get_recent_results(ticker: str, limit: int = 20) -> list[dict[str, Any]]:
    """Последние закрытые пары BUY→SELL по тикеру."""
    engine = _engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, ts, side, quantity, price, signal_type
                FROM public.trade_history
                WHERE ticker = :ticker AND strategy_name = :strategy
                ORDER BY ts ASC, id ASC
            """),
            {"ticker": ticker, "strategy": GAME_5M_STRATEGY},
        ).fetchall()

    result = []
    i = 0
    while i < len(rows):
        r = rows[i]
        if r[2] != "BUY":
            i += 1
            continue
        buy_id, buy_ts, _, qty, entry_price, entry_signal = r
        entry_price = float(entry_price)
        qty = float(qty)
        j = i + 1
        while j < len(rows) and rows[j][2] != "SELL":
            j += 1
        if j >= len(rows):
            break
        sell_row = rows[j]
        exit_ts = sell_row[1]
        exit_price = float(sell_row[4])
        exit_signal = sell_row[5]
        try:
            log_ret = math.log(exit_price / entry_price)
            pnl_pct = float(log_ret * 100.0 - 2 * COMMISSION_RATE * 100.0)
        except Exception:
            pnl_pct = None
        result.append({
            "id": buy_id,
            "entry_ts": buy_ts,
            "entry_price": entry_price,
            "quantity": qty,
            "entry_signal_type": entry_signal,
            "exit_ts": exit_ts,
            "exit_price": exit_price,
            "exit_signal_type": exit_signal,
            "pnl_pct": pnl_pct,
        })
        i = j + 1

    result.reverse()
    return result[:limit]


def should_close_position(open_position: dict, current_decision: str, current_price: Optional[float]) -> tuple[bool, str]:
    """Закрывать ли позицию: по сигналу SELL или по истечении MAX_POSITION_DAYS."""
    if current_price is None or current_price <= 0:
        return False, ""

    entry_ts = open_position.get("entry_ts")
    if isinstance(entry_ts, datetime):
        age = datetime.now() - entry_ts
    else:
        age = timedelta(0)
    if age > timedelta(days=MAX_POSITION_DAYS):
        return True, "TIME_EXIT"
    if current_decision == "SELL":
        return True, "SELL"
    return False, ""

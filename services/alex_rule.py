"""
Правило Алекса для SNDK (дневной вход):

- VIX < 20
- Вчера был «красный» день (close_{t-1} < close_{t-2})
- Сегодня пробой вверх: текущая цена > close вчера

Используется для согласования с 5m-рекомендацией: 5m даёт интрадей-сигнал,
правило Алекса — дневной контекст (входить на пробое после падения при спокойном VIX).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import create_engine, text

from config_loader import get_database_url

logger = logging.getLogger(__name__)

VIX_THRESHOLD = 20.0


def get_alex_rule_status(ticker: str, current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Проверяет условия правила Алекса для SNDK по последним дневным данным.

    current_price: текущая цена (из 5m или quotes); если None, берётся последний close из quotes.

    Returns:
        None если тикер не SNDK или нет данных.
        Иначе dict: vix, vix_ok, yesterday_red, breakout_today, message, entry_conditions_met.
    """
    if ticker.upper() != "SNDK":
        return None
    engine = create_engine(get_database_url())
    try:
        with engine.connect() as conn:
            # Последние 3 дня SNDK (от новых к старым)
            rows = conn.execute(
                text(
                    """
                    SELECT date, close
                    FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC
                    LIMIT 3
                    """
                ),
                {"ticker": ticker},
            ).fetchall()
            if not rows or len(rows) < 2:
                return None
            # rows[0] = последний закрытый день, rows[1] = позавчера от него
            close_last = float(rows[0][1])  # вчера (или последний доступный день)
            close_prev = float(rows[1][1])  # позавчера
            price_today = current_price if current_price is not None else close_last

            vix_row = conn.execute(
                text(
                    "SELECT close FROM quotes WHERE ticker = '^VIX' ORDER BY date DESC LIMIT 1"
                ),
            ).fetchone()
            vix = float(vix_row[0]) if vix_row and vix_row[0] is not None else None
    except Exception as e:
        logger.debug("Alex rule check %s: %s", ticker, e)
        return None

    vix_ok = vix is not None and vix < VIX_THRESHOLD
    yesterday_red = close_last < close_prev
    breakout_today = price_today > close_last
    entry_conditions_met = vix_ok and yesterday_red and breakout_today

    parts = []
    if vix is not None:
        parts.append(f"VIX {vix:.1f} {'< 20 ✓' if vix_ok else '≥ 20'}")
    parts.append(f"вчера красный: {'✓' if yesterday_red else 'нет'}")
    parts.append(f"пробой вверх (цена > вчера): {'✓' if breakout_today else 'нет'}")
    message = "Правило Алекса (дневное): " + ", ".join(parts)
    if entry_conditions_met:
        message += " → условия входа выполнены."
    else:
        message += " → ждём выполнения или уже в позиции."

    return {
        "vix": vix,
        "vix_ok": vix_ok,
        "yesterday_red": yesterday_red,
        "breakout_today": breakout_today,
        "entry_conditions_met": entry_conditions_met,
        "message": message,
        "close_yesterday": close_last,
        "price_today": price_today,
    }

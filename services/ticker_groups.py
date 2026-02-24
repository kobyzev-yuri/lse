"""
Группы тикеров по стилю игры.

- Быстрая игра (интрадей, 5m): SNDK, NDK, LITE, NBIS — короткие интервалы, 5-минутные индикаторы.
- Средние дистанции (смешанный вариант): AMD — среднесрочный горизонт.
- Вдолгую (свинг, дневные): MSFT, ORCL — дневные/недельные решения.

Конфиг (config.env):
  TICKERS_FAST=SNDK,NDK,LITE,NBIS
  TICKERS_MEDIUM=AMD
  TICKERS_LONG=MSFT,ORCL
"""

from __future__ import annotations

from typing import List

from config_loader import get_config_value

DEFAULT_TICKERS_FAST = "SNDK,NDK,LITE,NBIS"
DEFAULT_TICKERS_MEDIUM = "AMD"
DEFAULT_TICKERS_LONG = "MSFT,ORCL"


def get_tickers_fast() -> List[str]:
    """Тикеры для быстрой игры (5m, интрадей)."""
    raw = get_config_value("TICKERS_FAST", DEFAULT_TICKERS_FAST) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_tickers_medium() -> List[str]:
    """Тикеры для средних дистанций (смешанный вариант)."""
    raw = get_config_value("TICKERS_MEDIUM", DEFAULT_TICKERS_MEDIUM) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_tickers_long() -> List[str]:
    """Тикеры для игры вдолгую (свинг, дневные решения)."""
    raw = get_config_value("TICKERS_LONG", DEFAULT_TICKERS_LONG) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_all_ticker_groups() -> List[str]:
    """Объединённый список: быстрые → средние → долгие, без дубликатов."""
    fast = get_tickers_fast()
    medium = get_tickers_medium()
    long_ = get_tickers_long()
    seen = set()
    result = []
    for t in fast + medium + long_:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result

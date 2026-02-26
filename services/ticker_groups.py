"""
Группы тикеров по стилю игры (совпадают с зарегистрированными в БД /tickers).

- Быстрая игра (5m, интрадей): тикеры с 5m данными, короткие интервалы.
- Средние дистанции: среднесрочный горизонт.
- Вдолгую: свинг, дневные/недельные решения (акции, forex, товары).

Конфиг (config.env): TICKERS_FAST, TICKERS_MEDIUM, TICKERS_LONG.
"""

from __future__ import annotations

from typing import List

from config_loader import get_config_value

# Дефолты: распределение по группам (ваши зарегистрированные тикеры)
DEFAULT_TICKERS_FAST = "SNDK,LITE"
DEFAULT_TICKERS_MEDIUM = "ALAB,MU,TER,AMD"
DEFAULT_TICKERS_LONG = "MSFT,GBPUSD=X,GC=F,^VIX"


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


def get_tickers_for_portfolio_game() -> List[str]:
    """Тикеры для портфельной игры (trading_cycle_cron).
    Если задан TRADING_CYCLE_TICKERS в config.env — используем его; иначе MEDIUM + LONG."""
    raw = get_config_value("TRADING_CYCLE_TICKERS", "").strip()
    if raw:
        return [t.strip() for t in raw.split(",") if t.strip()]
    medium = get_tickers_medium()
    long_ = get_tickers_long()
    seen = set()
    result = []
    for t in medium + long_:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result

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
DEFAULT_TICKERS_FAST = "SNDK,MU,LITE,MSFT"
# Medium horizon / drivers for KB + forecasts (not 5m game unless also in TICKERS_FAST): semis, OEM, megacap tech names.
DEFAULT_TICKERS_MEDIUM = "ALAB,TER,AMD,ANET,INTC,DELL,AVGO,ORCL,PLTR"
# CL=F — WTI; BZ=F — Brent. Мегакап + макро + драйверы шефа (META, GOOGL, ANET, INTC, DELL, ALAB, AVGO, NVDA, ORCL, PLTR, AMD).
DEFAULT_TICKERS_LONG = (
    "MSFT,META,GOOGL,AMZN,NVDA,ANET,INTC,DELL,ALAB,AVGO,ORCL,PLTR,AMD,SNDK,"
    "GBPUSD=X,GC=F,^VIX,CL=F,BZ=F"
)

# Быстрая игра 5m: целевые стоки для daily (Alex: SNDK, NBIS — лидеры; ASML, MU — AI bottlenecks; LITE, CIEN — волатильные)
DEFAULT_GAME_5M_FAST = "SNDK,NBIS,ASML,MU,LITE,CIEN"
# Correlation “all vs all”: megacap + semis + VIX/oil/gold/forex + extra drivers (ANET, INTC, DELL, …) for LLM/matrix context.
DEFAULT_GAME_5M_CORRELATION_CONTEXT = (
    "MSFT,META,GOOGL,AMZN,NVDA,SMH,QQQ,TLT,^VIX,CL=F,GC=F,GBPUSD=X,"
    "ANET,INTC,DELL,ALAB,AVGO,ORCL,PLTR,AMD"
)


def get_tickers_fast() -> List[str]:
    """Тикеры для быстрой игры (5m, интрадей). Используются для /chart5m, /recommend5m, списков."""
    raw = get_config_value("TICKERS_FAST", DEFAULT_TICKERS_FAST) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_tickers_game_5m() -> List[str]:
    """Тикеры, по которым крон запускает игру 5m (вход/выход, GAME_5M).
    Если задан GAME_5M_TICKERS в config.env — только они; иначе TICKERS_FAST.
    Позволяет исключить тикер из игры (например LITE), оставив его в TICKERS_FAST для графиков."""
    raw = get_config_value("GAME_5M_TICKERS", "").strip()
    if raw:
        return [t.strip() for t in raw.split(",") if t.strip()]
    return get_tickers_fast()


def get_game_5m_correlation_context() -> List[str]:
    """Тикеры-контекст для корреляции с игрой 5m: фон (MSFT, META, AMZN) и индикаторы (NVDA, SMH, QQQ, VIX, нефть, forex).
    Не торгуем ими в 5m; используем для матрицы «все со всеми» при решении по быстрым стокам.
    config.env: GAME_5M_CORRELATION_CONTEXT (пусто = дефолт из DEFAULT_GAME_5M_CORRELATION_CONTEXT)."""
    raw = get_config_value("GAME_5M_CORRELATION_CONTEXT", DEFAULT_GAME_5M_CORRELATION_CONTEXT) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_tickers_for_5m_correlation() -> List[str]:
    """Универс для матрицы корреляции (игра 5m + LLM/крон): без дублей.

    Состав:
    1) тикеры игры 5m (`GAME_5M_TICKERS` / `TICKERS_FAST`);
    2) портфельный цикл — как `/corr`: `TRADING_CYCLE_TICKERS` или MEDIUM+LONG;
    3) `GAME_5M_CORRELATION_CONTEXT` (фон, VIX, нефть, forex и т.д.).

    Тогда по каждому тикеру игры в промпте видны корреляции со **всеми** остальными
    (портфель + макро), а не только внутри шестёрки игры.

    Отключить портфель из универсa (старое поведение: только игра + контекст):
    `GAME_5M_CORRELATION_EXCLUDE_PORTFOLIO=true` в config.env.
    """
    game = get_tickers_game_5m()
    context = get_game_5m_correlation_context()
    raw_ex = (get_config_value("GAME_5M_CORRELATION_EXCLUDE_PORTFOLIO", "") or "").strip().lower()
    exclude_pf = raw_ex in ("1", "true", "yes")
    portfolio = [] if exclude_pf else get_tickers_for_portfolio_game()
    seen: set = set()
    result: List[str] = []
    for t in game + portfolio + context:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def get_tickers_medium() -> List[str]:
    """Тикеры для средних дистанций (смешанный вариант)."""
    raw = get_config_value("TICKERS_MEDIUM", DEFAULT_TICKERS_MEDIUM) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_tickers_long() -> List[str]:
    """Тикеры для игры вдолгую (свинг, дневные решения)."""
    raw = get_config_value("TICKERS_LONG", DEFAULT_TICKERS_LONG) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_oil_ticker() -> str:
    """Тикер нефти для индикатора геополитики и торговли (WTI — CL=F). Можно переопределить через OIL_TICKER в config.env."""
    raw = get_config_value("OIL_TICKER", "CL=F").strip()
    return raw or "CL=F"


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


def get_tracked_tickers_for_kb() -> List[str]:
    """Тикеры «нашего» списка для knowledge_base (FAST+MEDIUM+LONG + MACRO/US_MACRO).
    Фильтрация при записи включается только если `KB_INGEST_TRACKED_TICKERS_ONLY=true` (см. `kb_ingest_tracked_tickers_only`)."""
    allowed = {"MACRO", "US_MACRO"}
    for t in get_all_ticker_groups():
        allowed.add(t.strip())
    return list(allowed)


def kb_ingest_tracked_tickers_only() -> bool:
    """
    Если True — при сохранении в KB (Alpha Vantage earnings/news, LLM-новости) отбрасывать тикеры вне get_tracked_tickers_for_kb().
    Если False (по умолчанию) — сохранять всё входящее; сентимент и отбор под LLM — позже.
    Investing.com: см. INVESTING_NEWS_STRICT_TRACKED_ONLY (по умолчанию несохранённые матчи идут как MACRO).
    """
    raw = (get_config_value("KB_INGEST_TRACKED_TICKERS_ONLY", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes")


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


def get_tickers_indicator_only() -> List[str]:
    """Тикеры только как индикаторы (контекст, корреляция): по ним не открываем позиции в портфеле.
    config.env: TICKERS_INDICATOR_ONLY (например ^VIX). Пусто — используем правило: тикеры с ^ в портфеле считаем индикаторами."""
    raw = get_config_value("TICKERS_INDICATOR_ONLY", "").strip()
    if raw:
        return [t.strip() for t in raw.split(",") if t.strip()]
    portfolio = get_tickers_for_portfolio_game()
    return [t for t in portfolio if t.startswith("^")]

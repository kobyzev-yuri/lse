"""
Учёт открытия и закрытия биржи (NYSE) и праздников для решений 5m.

Моменты открытия и закрытия биржи, а также дни праздников и прилегающие к ним дни
рассматриваются отдельно: в эти периоды процессы новостей и ликвидности особые,
агент должен учитывать это при принятии решения.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

# NYSE: регулярная сессия по Eastern Time
NYSE_OPEN_TIME = time(9, 30)   # 09:30 ET
NYSE_CLOSE_TIME = time(16, 0)  # 16:00 ET
# Окно «особого режима» вокруг открытия/закрытия (минуты)
NEAR_OPEN_MINUTES = 60   # первый час после открытия
NEAR_CLOSE_MINUTES = 60  # последний час перед закрытием

# Часовой пояс биржи
try:
    from zoneinfo import ZoneInfo
    NYSE_TZ = ZoneInfo("America/New_York")
except ImportError:
    NYSE_TZ = None  # fallback: use UTC and warn


def _get_nyse_holidays_for_year(year: int) -> Set[date]:
    """Даты праздников NYSE в данном году (только полные выходные, без early close)."""
    holidays: Set[date] = set()

    # Фиксированные (если не выходной)
    for month, day in [(1, 1), (7, 4), (6, 19), (12, 25)]:
        d = date(year, month, day)
        if d.weekday() < 5:  # пн-пт
            holidays.add(d)
        # July 4 observed on next weekday if weekend — упрощённо не трогаем
        # Juneteenth similar

    # 3-й понедельник января — MLK
    jan = date(year, 1, 1)
    mondays = [d for d in (jan + timedelta(days=i) for i in range(31)) if d.month == 1 and d.weekday() == 0]
    if len(mondays) >= 3:
        holidays.add(mondays[2])

    # 3-й понедельник февраля — Presidents
    feb = date(year, 2, 1)
    mondays = [d for d in (feb + timedelta(days=i) for i in range(28)) if d.month == 2 and d.weekday() == 0]
    if len(mondays) >= 3:
        holidays.add(mondays[2])

    # Последний понедельник мая — Memorial
    may = date(year, 5, 31)
    for i in range(7):
        d = may - timedelta(days=i)
        if d.weekday() == 0:
            holidays.add(d)
            break

    # 1-й понедельник сентября — Labor
    sep = date(year, 9, 1)
    for i in range(7):
        d = sep + timedelta(days=i)
        if d.weekday() == 0:
            holidays.add(d)
            break

    # 4-й четверг ноября — Thanksgiving
    nov = date(year, 11, 1)
    thurs = [d for d in (nov + timedelta(days=i) for i in range(30)) if d.month == 11 and d.weekday() == 3]
    if len(thurs) >= 4:
        holidays.add(thurs[3])

    # Good Friday (приближённо: пятница перед Easter; для 2024–2026 зафиксируем)
    good_fridays = {
        2024: date(2024, 3, 29),
        2025: date(2025, 4, 18),
        2026: date(2026, 4, 3),
    }
    if year in good_fridays:
        holidays.add(good_fridays[year])

    return holidays


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


_HOLIDAYS_CACHE: Dict[int, Set[date]] = {}


def _is_holiday(d: date) -> bool:
    if _is_weekend(d):
        return False
    if d.year not in _HOLIDAYS_CACHE:
        _HOLIDAYS_CACHE[d.year] = _get_nyse_holidays_for_year(d.year)
    return d in _HOLIDAYS_CACHE[d.year]


def get_market_session_context(dt_utc: datetime | None = None) -> Dict[str, Any]:
    """
    Контекст сессии биржи для текущего (или переданного) момента времени.
    Используется агентом 5m для отдельного учёта открытия/закрытия и праздников.

    Returns:
        dict:
        - market_tz: "America/New_York"
        - et_now: текущее время в ET (строка)
        - date_et: дата в ET
        - is_near_open: первый час после открытия (особый поток новостей/ликвидности)
        - is_near_close: последний час перед закрытием (особый режим)
        - is_holiday: сегодня праздник NYSE
        - is_day_before_holiday: завтра праздник или выходной
        - is_day_after_holiday: вчера был праздник или выходной
        - session_phase: "NEAR_OPEN" | "NEAR_CLOSE" | "REGULAR" | "PRE_MARKET" | "AFTER_HOURS" | "WEEKEND" | "HOLIDAY"
        - session_note_ru: краткая подсказка для агента на русском
    """
    if dt_utc is None:
        dt_utc = datetime.utcnow()
    if NYSE_TZ is None:
        # Fallback без zoneinfo: считаем, что сервер в UTC, ET = UTC-5 (зима) — грубо
        logger.warning("zoneinfo недоступен, контекст сессии биржи приблизительный")
        et_now = dt_utc - timedelta(hours=5)
    else:
        # datetime.utcnow() — naive UTC; делаем aware и переводим в ET
        from datetime import timezone
        utc_aware = dt_utc.replace(tzinfo=timezone.utc) if dt_utc.tzinfo is None else dt_utc
        et_now = utc_aware.astimezone(NYSE_TZ)

    date_et = et_now.date()

    is_weekend = _is_weekend(date_et)
    is_holiday = not is_weekend and _is_holiday(date_et)
    is_day_before_holiday = _is_holiday(date_et + timedelta(days=1)) or _is_weekend(date_et + timedelta(days=1))
    is_day_after_holiday = _is_holiday(date_et - timedelta(days=1)) or _is_weekend(date_et - timedelta(days=1))

    time_et_only = et_now.time() if hasattr(et_now, "time") else time_only
    if getattr(time_et_only, "tzinfo", None):
        time_et_only = time_et_only.replace(tzinfo=None)  # type: ignore[union-attr]
    open_naive = datetime.combine(date_et, NYSE_OPEN_TIME)
    close_naive = datetime.combine(date_et, NYSE_CLOSE_TIME)
    near_open_end_time = (open_naive + timedelta(minutes=NEAR_OPEN_MINUTES)).time()
    near_close_start_time = (close_naive - timedelta(minutes=NEAR_CLOSE_MINUTES)).time()

    is_near_open = False
    is_near_close = False
    session_phase = "REGULAR"

    if is_weekend:
        session_phase = "WEEKEND"
    elif is_holiday:
        session_phase = "HOLIDAY"
    else:
        if time_et_only < NYSE_OPEN_TIME:
            session_phase = "PRE_MARKET"
        elif time_et_only >= NYSE_CLOSE_TIME:
            session_phase = "AFTER_HOURS"
        else:
            if NYSE_OPEN_TIME <= time_et_only < near_open_end_time:
                is_near_open = True
                session_phase = "NEAR_OPEN"
            elif near_close_start_time <= time_et_only < NYSE_CLOSE_TIME:
                is_near_close = True
                session_phase = "NEAR_CLOSE"
            else:
                session_phase = "REGULAR"

    # Краткая подсказка для агента
    notes = []
    if session_phase == "NEAR_OPEN":
        notes.append("Открытие биржи — особый поток новостей и ликвидности")
    elif session_phase == "NEAR_CLOSE":
        notes.append("Закрытие биржи — особый режим, учитывать при решении")
    elif session_phase == "HOLIDAY":
        notes.append("Праздник NYSE — биржа закрыта")
    elif session_phase == "WEEKEND":
        notes.append("Выходной — биржа закрыта")
    if is_day_before_holiday and session_phase not in ("HOLIDAY", "WEEKEND"):
        notes.append("Завтра праздник/выходной — возможны ранние закрытия или сдвиг новостей")
    if is_day_after_holiday and session_phase not in ("HOLIDAY", "WEEKEND"):
        notes.append("Вчера был праздник/выходной — наверстывание реакции на новости")

    session_note_ru = " ".join(notes) if notes else "Обычная сессия"

    return {
        "market_tz": "America/New_York",
        "et_now": et_now.strftime("%Y-%m-%d %H:%M"),
        "date_et": date_et.isoformat(),
        "is_near_open": is_near_open,
        "is_near_close": is_near_close,
        "is_holiday": is_holiday,
        "is_day_before_holiday": is_day_before_holiday,
        "is_day_after_holiday": is_day_after_holiday,
        "session_phase": session_phase,
        "session_note_ru": session_note_ru,
    }

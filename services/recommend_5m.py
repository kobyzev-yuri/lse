"""
Рекомендация по 5-минутным данным для решения агента (игра 5m, сигналы).

Для решения используются все доступные 5m-отметки от текущего момента назад на 7 дней
(или сколько отдаёт Yahoo). Эти данные вместе с контекстом из KB и запросом о свежих
новостях (LLM) передаются агенту для принятия решения BUY/HOLD/SELL.

- свечи 5m: полное окно до MAX_DAYS_5M=7 дней (yfinance);
- по данным: RSI, волатильность, импульс 2ч, high/low за окно (где хватает баров);
- решение и параметры стоп/тейк под интрадей;
- опционально: запрос к LLM о свежих новостях/настроениях перед решением (USE_LLM_NEWS).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Версия логики принятия решения в get_decision_5m (для фиксации в context_json сделки)
GAME_5M_RULE_VERSION = "2026-04-17"

# Ветка технического входа → краткое обоснование стратегии (не прогноз цены, а смысл правила).
ENTRY_BRANCH_INTUITION: Dict[str, str] = {
    "strong_buy_rsi": (
        "Интуиция: зона глубокой перепроданности на 5m при ограничении на силу недавнего снижения за 2ч. "
        "Это не предсказание разворота на следующем баре, а допустимый коридор для краткосрочного long: "
        "RSI отражает уже произошедшую распродажу на окне, порог по 2ч отсекает сценарий ускоренного обвала."
    ),
    "buy_5d_low": (
        "Интуиция: цена у нижней границы нескольких дней при умеренно низком RSI — вход у опорной зоны диапазона, "
        "а не ставка на продолжение тренда вниз без фильтров."
    ),
    "buy_rth_momentum": (
        "Интуиция: в текущей RTH-сессии уже зафиксирован положительный импульс, RSI ещё не перекуплен — "
        "вход по силе дня, а не по «отскоку после ливня»."
    ),
    "buy_premarket_momentum": (
        "Интуиция: ранний день, мало 5m-баров регулярной сессии; положительный импульс по премаркету 1m — "
        "осторожный вход до накопления полноценной картины RTH."
    ),
    "buy_cross_day_2h": (
        "Интуиция: разрешён кросс-дневной импульс за 2ч — покупка на продолжении краткосрочного движения вверх "
        "при RSI ниже порога перекупленности (см. GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY)."
    ),
    "buy_news_support": (
        "Интуиция: при нейтральном RSI позитивный фон в KB снижает барьер для осторожного long — "
        "контекст новостей в сторону покупки без технического экстремума."
    ),
}


def _build_5m_entry_explanation(
    *,
    branch: str,
    decision: str,
    decision_rule_params: Dict[str, Any],
    feats: Dict[str, Any],
    strong_buy_downgraded: bool,
) -> Dict[str, Optional[str]]:
    """Формальное условие ветки + текст интуиции для алерта и context_json."""
    drp = decision_rule_params
    rsi = feats.get("rsi_5m")
    mom2 = feats.get("momentum_2h_pct")
    price = feats.get("price")
    low5 = feats.get("low_5d")
    mom_rth = feats.get("momentum_rth_today_pct")
    mom_rth_bars = feats.get("momentum_rth_today_bars")
    pm_id = feats.get("premarket_intraday_momentum_pct")
    wmin = feats.get("momentum_rth_today_window_min")

    def _f(v: Any, nd: int = 2) -> str:
        if v is None:
            return "—"
        try:
            return format(float(v), f".{nd}f")
        except (TypeError, ValueError):
            return str(v)

    condition: Optional[str] = None
    if branch == "strong_buy_rsi":
        rsi_mx = drp.get("rsi_strong_buy_max")
        mom_min = drp.get("momentum_for_strong_buy_min")
        condition = (
            f"Условие (ветка RSI + импульс 2ч): RSI(5m) ≤ {_f(rsi_mx, 1)}, импульс 2ч ≥ {_f(mom_min)}%. "
            f"Факт: RSI {_f(rsi, 1)}, импульс {_f(mom2)}%."
        )
    elif branch == "buy_5d_low":
        rsi_mx = drp.get("rsi_buy_max")
        mult = drp.get("price_to_low5d_multiplier_max")
        thr = None
        try:
            if low5 is not None and mult is not None:
                thr = float(low5) * float(mult)
        except (TypeError, ValueError):
            thr = None
        condition = (
            f"Условие (ветка у 5д low): RSI(5m) ≤ {_f(rsi_mx, 1)}, цена ≤ low_5d×{_f(mult, 2)}. "
            f"Факт: RSI {_f(rsi, 1)}, цена ${_f(price)}, low_5d ${_f(low5)}, порог цены ${_f(thr)}."
        )
    elif branch == "buy_rth_momentum":
        rth_min = drp.get("momentum_buy_min")
        min_sess = drp.get("momentum_min_session_bars")
        rsi_cap = drp.get("rsi_for_momentum_buy_max")
        condition = (
            f"Условие (ветка импульс RTH): импульс сессии > {_f(rth_min)}%, "
            f"число 5m-баров сессии ≥ {min_sess}, RSI(5m) < {_f(rsi_cap, 1)}. "
            f"Факт: RTH {_f(mom_rth)}% (~{_f(wmin, 0)} мин), баров {mom_rth_bars}, RSI {_f(rsi, 1)}."
        )
    elif branch == "buy_premarket_momentum":
        pm_hi = drp.get("premarket_momentum_block_below")
        pm_lo = drp.get("premarket_momentum_buy_min")
        min_sess = drp.get("momentum_min_session_bars")
        rsi_cap = drp.get("rsi_for_momentum_buy_max")
        condition = (
            f"Условие (ветка премаркет 1m): импульс премаркета > {_f(pm_lo)}% и ≥ {_f(pm_hi)}%, "
            f"баров 5m сессии < {min_sess}, RSI(5m) < {_f(rsi_cap, 1)}. "
            f"Факт: премаркет {_f(pm_id)}%, баров {mom_rth_bars}, RSI {_f(rsi, 1)}."
        )
    elif branch == "buy_cross_day_2h":
        rth_min = drp.get("momentum_buy_min")
        rsi_cap = drp.get("rsi_for_momentum_buy_max")
        condition = (
            f"Условие (ветка кросс-день 2ч): импульс 2ч > {_f(rth_min)}%, RSI(5m) < {_f(rsi_cap, 1)}, "
            f"MOMENTUM_ALLOW_CROSS_DAY_BUY включён. Факт: импульс {_f(mom2)}%, RSI {_f(rsi, 1)}."
        )
    elif branch == "buy_news_support":
        condition = (
            f"Условие (ветка новости + нейтральный RSI): RSI 38…52, импульс 2ч ≥ −0.5%, позитив в KB. "
            f"Факт: RSI {_f(rsi, 1)}, импульс {_f(mom2)}%."
        )
    else:
        condition = "Условие: см. reasoning и decision_rule_params."

    intuition = ENTRY_BRANCH_INTUITION.get(
        branch,
        "Интуиция: правило из набора интрадей-условий 5m; детали — в reasoning и decision_rule_params.",
    )
    if branch == "strong_buy_rsi" and decision == "BUY" and strong_buy_downgraded:
        intuition = (
            intuition
            + " Итоговое решение BUY (не STRONG_BUY): техническое ядро было перепродано сильнее, "
            "но негативный фон в новостях ослабил сигнал."
        )
    return {"condition": condition, "intuition": intuition}

# Макс. длина content в контексте KB (чтобы не раздувать ответ)
KB_NEWS_CONTENT_MAX_LEN = 500

# Максимум дней 5m по ограничениям Yahoo
MAX_DAYS_5M = 7
# 30m: у Yahoo обычно больший горизонт, чем у 5m; верхняя граница для fetch_30m_ohlc
MAX_DAYS_30M = 60
# Период RSI по 5m свечам (14 свечей ≈ 70 мин)
RSI_PERIOD_5M = 14
# Баров в «2 часа» для импульса
BARS_2H = 24
# То же окно ~2 ч на 30m-сетке (сверка / эмуляция GAME_5M на 30m)
BARS_2H_30M = 4

# Регулярная сессия US (NYSE/NASDAQ) в ET
US_SESSION_START = (9, 30)  # 9:30
US_SESSION_END = (16, 0)    # 16:00


def _cfg_float_bracket(
    _gcv,
    key: str,
    default: float,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
) -> float:
    try:
        v = float((_gcv(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def get_decision_5m_rule_thresholds() -> Dict[str, Any]:
    """
    Пороги технического решения 5m из config.env (раньше были константами в get_decision_5m).
    Единый снимок для context_json, анализатора и документации.
    """
    from config_loader import get_config_value as _gcv

    vol_wait = _cfg_float_bracket(_gcv, "GAME_5M_VOLATILITY_WAIT_MIN", 0.7, 0.3, 2.0)
    try:
        sell_confirm_bars = int((_gcv("GAME_5M_SELL_CONFIRM_BARS", "2") or "2").strip())
    except (ValueError, TypeError):
        sell_confirm_bars = 2
    sell_confirm_bars = max(1, min(5, sell_confirm_bars))
    try:
        min_sess_bars = int((_gcv("GAME_5M_MOMENTUM_MIN_SESSION_BARS", "7") or "7").strip())
    except (ValueError, TypeError):
        min_sess_bars = 7
    min_sess_bars = max(2, min(48, min_sess_bars))
    pm_mom_buy_min = _cfg_float_bracket(_gcv, "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN", 0.5, -5.0, 10.0)
    pm_mom_block_below = _cfg_float_bracket(_gcv, "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW", -2.0, -20.0, 10.0)

    rsi_strong_buy_max = _cfg_float_bracket(_gcv, "GAME_5M_RSI_STRONG_BUY_MAX", 32.0, 5.0, 55.0)
    momentum_for_strong_buy_min = _cfg_float_bracket(_gcv, "GAME_5M_MOMENTUM_STRONG_BUY_MIN", -0.3, -5.0, 5.0)
    rsi_buy_max = _cfg_float_bracket(_gcv, "GAME_5M_RSI_BUY_MAX", 38.0, 5.0, 60.0)
    price_to_low5d_multiplier_max = _cfg_float_bracket(_gcv, "GAME_5M_PRICE_TO_LOW5D_MULT_MAX", 1.005, 1.0001, 1.05)
    rsi_sell_min = _cfg_float_bracket(_gcv, "GAME_5M_RSI_SELL_MIN", 76.0, 50.0, 92.0)
    rsi_hold_overbought_min = _cfg_float_bracket(_gcv, "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN", 68.0, 40.0, 88.0)
    rth_momentum_buy_min = _cfg_float_bracket(_gcv, "GAME_5M_RTH_MOMENTUM_BUY_MIN", 0.5, 0.05, 10.0)
    rsi_for_momentum_buy_max = _cfg_float_bracket(_gcv, "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX", 62.0, 35.0, 85.0)
    volatility_warn_buy_min = _cfg_float_bracket(_gcv, "GAME_5M_VOLATILITY_WARN_BUY_MIN", 0.4, 0.05, 10.0)

    return {
        "volatility_wait_min": vol_wait,
        "sell_confirm_bars": sell_confirm_bars,
        "momentum_min_session_bars": min_sess_bars,
        "premarket_momentum_buy_min": pm_mom_buy_min,
        "premarket_momentum_block_below": pm_mom_block_below,
        "rsi_strong_buy_max": rsi_strong_buy_max,
        "momentum_for_strong_buy_min": momentum_for_strong_buy_min,
        "rsi_buy_max": rsi_buy_max,
        "price_to_low5d_multiplier_max": price_to_low5d_multiplier_max,
        "rsi_sell_min": rsi_sell_min,
        "rsi_hold_overbought_min": rsi_hold_overbought_min,
        "momentum_buy_min": rth_momentum_buy_min,
        "rsi_for_momentum_buy_max": rsi_for_momentum_buy_max,
        "volatility_warn_buy_min": volatility_warn_buy_min,
    }


def filter_to_last_n_us_sessions(
    df: Optional[pd.DataFrame],
    n: int,
) -> Optional[pd.DataFrame]:
    """
    Оставляет только бары внутри американской регулярной сессии (9:30–16:00 ET)
    за последние n сессий. Убирает календарные «хвосты» и смену дат по Москве.

    df должен иметь колонку datetime (желательно уже в ET).
    """
    if df is None or df.empty or "datetime" not in df.columns:
        return df
    from datetime import time as dt_time
    df = df.copy()
    dt = pd.to_datetime(df["datetime"])
    if dt.dt.tz is None:
        try:
            dt = dt.dt.tz_localize("America/New_York", ambiguous=True)
        except Exception:
            dt = dt.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
    else:
        dt = dt.dt.tz_convert("America/New_York")
    start = dt_time(*US_SESSION_START)
    end = dt_time(*US_SESSION_END)
    mask = (dt.dt.time >= start) & (dt.dt.time <= end)
    df = df.loc[mask].copy()
    if df.empty:
        return df
    # Даты сессий по ET (присваиваем по позиции, чтобы индексы не ломали выравнивание)
    session_dates_et = dt.loc[mask].dt.date
    df = df.copy()
    df["_session"] = session_dates_et.values
    # Последние n уникальных дат
    unique_dates = sorted(df["_session"].unique(), reverse=True)[:n]
    df = df[df["_session"].isin(unique_dates)].copy()
    return df.sort_values("datetime").reset_index(drop=True)


def _prev_close_before_trade_date(ticker: str, trade_date: "date") -> Optional[float]:
    """Close последнего торгового дня строго до trade_date (quotes)."""
    try:
        from config_loader import get_database_url
        from sqlalchemy import create_engine, text

        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT close FROM quotes
                    WHERE ticker = :ticker AND date < :trade_date
                    ORDER BY date DESC
                    LIMIT 1
                    """
                ),
                {"ticker": ticker, "trade_date": trade_date},
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        logger.debug("prev_close before %s for %s: %s", trade_date, ticker, e)
    return None


def _compute_rth_open_gap_pct(
    df: Optional[pd.DataFrame],
    ticker: str,
    trade_date: Optional["date"] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Гэп первого RTH-бара торгового дня (ET) к вчерашнему close, %.
    Returns (gap_pct, rth_open_price, prev_close).
    trade_date: календарный день ET; если None — последний день в df (live).
    """
    if df is None or df.empty or "datetime" not in df.columns:
        return None, None, None
    try:
        from datetime import date as date_type
        from datetime import time as dt_time

        dts_et = pd.to_datetime(df["datetime"])
        if dts_et.dt.tz is None:
            try:
                dts_et = dts_et.dt.tz_localize("America/New_York", ambiguous=True)
            except Exception:
                dts_et = dts_et.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
        else:
            dts_et = dts_et.dt.tz_convert("America/New_York")
        target_date = trade_date if trade_date is not None else dts_et.max().date()
        if not isinstance(target_date, date_type):
            target_date = pd.Timestamp(target_date).date()
        t_start = dt_time(*US_SESSION_START)
        t_end = dt_time(*US_SESSION_END)
        prev_close = _prev_close_before_trade_date(ticker, target_date)
        if prev_close is None or float(prev_close) <= 0:
            prev_mask = (dts_et.dt.date < target_date) & (dts_et.dt.time >= t_start) & (dts_et.dt.time <= t_end)
            prev_df = df.loc[prev_mask].sort_values("datetime")
            if not prev_df.empty:
                prev_close = float(prev_df["Close"].iloc[-1])
        if prev_close is None or float(prev_close) <= 0:
            return None, None, None
        prev_close = float(prev_close)
        rth_mask = (dts_et.dt.date == target_date) & (dts_et.dt.time >= t_start) & (dts_et.dt.time <= t_end)
        df_td = df.loc[rth_mask].sort_values("datetime")
        if df_td.empty:
            return None, None, prev_close
        if "Open" in df_td.columns:
            open_first = float(df_td["Open"].iloc[0])
        else:
            open_first = float(df_td["Close"].iloc[0])
        if open_first <= 0:
            return None, None, prev_close
        gap = round((open_first / prev_close - 1.0) * 100.0, 2)
        return gap, open_first, prev_close
    except Exception as e:
        logger.debug("rth_open_gap %s: %s", ticker, e)
        return None, None, None


def fetch_5m_ohlc(ticker: str, days: int = None) -> Optional[pd.DataFrame]:
    """
    Загружает все доступные 5-минутные OHLC от текущего момента назад на days дней
    (по умолчанию MAX_DAYS_5M=7 — максимум, что даёт Yahoo).

    Returns:
        DataFrame с колонками datetime, Open, High, Low, Close или None.
    """
    if days is None:
        days = MAX_DAYS_5M
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance не установлен")
        return None
    days = min(max(1, days), MAX_DAYS_5M)
    end_date = datetime.utcnow() + timedelta(days=1)
    start_date = datetime.utcnow() - timedelta(days=days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    t = yf.Ticker(ticker)

    def _normalize(df_in):
        if df_in is None or df_in.empty:
            return None
        df_in = df_in.rename_axis("datetime").reset_index()
        for c in ("Open", "High", "Low", "Close"):
            if c not in df_in.columns:
                return None
        return df_in

    def _to_us_eastern(df_in):
        """Приводит datetime к времени американской биржи (NYSE/NASDAQ) для единой шкалы в Telegram и вебе."""
        if df_in is None or "datetime" not in df_in.columns:
            return df_in
        try:
            d = pd.to_datetime(df_in["datetime"])
            if d.dt.tz is None:
                # Yahoo для US-акций часто отдаёт naive в Eastern; иначе пробуем UTC
                try:
                    d = d.dt.tz_localize("America/New_York", ambiguous="infer")
                except Exception:
                    d = d.dt.tz_localize("UTC", ambiguous="infer").dt.tz_convert("America/New_York")
            else:
                d = d.dt.tz_convert("America/New_York")
            df_in = df_in.copy()
            df_in["datetime"] = d
            return df_in
        except Exception as e:
            logger.debug("Приведение 5m к US/Eastern: %s", e)
            return df_in

    def _coverage_score(df_in: Optional[pd.DataFrame]) -> tuple:
        """
        Оцениваем «насколько длинный» ряд для сравнения вариантов загрузки.
        Возвращаем кортеж (уникальных_дней, строк) — лексикографически сравниваем.
        """
        if df_in is None or df_in.empty or "datetime" not in df_in.columns:
            return (0, 0)
        try:
            d = pd.to_datetime(df_in["datetime"], errors="coerce")
            unique_days = int(d.dt.date.nunique()) if hasattr(d, "dt") else 0
        except Exception:
            unique_days = 0
        return (unique_days, int(len(df_in)))

    try:
        df = t.history(start=start_str, end=end_str, interval="5m", auto_adjust=False)
    except (TypeError, KeyError, AttributeError) as e:
        logger.debug("yfinance history для %s (start/end): %s", ticker, e)
        df = None
    df = _normalize(df)
    df_best = _to_us_eastern(df) if df is not None else None
    best_score = _coverage_score(df_best)

    # Fallback/alt: Yahoo иногда отдаёт 5m «обрезанно» через start/end,
    # но нормально через period. ВАЖНО: не расширяем окно сверх запрошенного days,
    # иначе "days=1" превращается в 7d и графики/логика становятся неочевидными.
    periods: List[str] = []
    if days:
        d = int(days)
        if d >= 7:
            periods.extend(["7d", "5d", "2d", "1d"])
        elif d >= 5:
            periods.extend(["5d", "2d", "1d"])
        elif d >= 2:
            periods.extend([f"{d}d", "1d"])
        else:
            periods.extend(["1d"])
    else:
        periods.extend(["7d", "5d", "2d", "1d"])
    seen = set()
    periods = [p for p in periods if not (p in seen or seen.add(p))]
    for period in periods:
        try:
            df = t.history(period=period, interval="5m", auto_adjust=False)
            df = _normalize(df)
            if df is not None and not df.empty:
                df2 = _to_us_eastern(df)
                sc = _coverage_score(df2)
                if sc > best_score:
                    df_best, best_score = df2, sc
                    logger.info("5m данные %s выбраны через period=%s (coverage=%s)", ticker, period, sc)
        except Exception as e:
            logger.debug("yfinance period=%s для %s: %s", period, ticker, e)
    if df_best is not None and not df_best.empty:
        return df_best
    logger.warning("Нет 5m данных для %s за %d дн. (Yahoo пустой ответ или биржа закрыта)", ticker, days)
    return None


def fetch_30m_ohlc(ticker: str, days: int = None) -> Optional[pd.DataFrame]:
    """
    30-минутные OHLC от Yahoo (yfinance). Горизонт по умолчанию до MAX_DAYS_30M календарных дней
    (фактически для бэктеста обычно берут столько же дней, сколько и для 5m, например 7).
    """
    if days is None:
        days = min(MAX_DAYS_5M, MAX_DAYS_30M)
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance не установлен")
        return None
    days = min(max(1, days), MAX_DAYS_30M)
    end_date = datetime.utcnow() + timedelta(days=1)
    start_date = datetime.utcnow() - timedelta(days=days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    t = yf.Ticker(ticker)

    def _normalize(df_in):
        if df_in is None or df_in.empty:
            return None
        df_in = df_in.rename_axis("datetime").reset_index()
        for c in ("Open", "High", "Low", "Close"):
            if c not in df_in.columns:
                return None
        return df_in

    def _to_us_eastern(df_in):
        if df_in is None or "datetime" not in df_in.columns:
            return df_in
        try:
            d = pd.to_datetime(df_in["datetime"])
            if d.dt.tz is None:
                try:
                    d = d.dt.tz_localize("America/New_York", ambiguous="infer")
                except Exception:
                    d = d.dt.tz_localize("UTC", ambiguous="infer").dt.tz_convert("America/New_York")
            else:
                d = d.dt.tz_convert("America/New_York")
            df_in = df_in.copy()
            df_in["datetime"] = d
            return df_in
        except Exception as e:
            logger.debug("Приведение 30m к US/Eastern: %s", e)
            return df_in

    try:
        df = t.history(start=start_str, end=end_str, interval="30m", auto_adjust=False)
    except (TypeError, KeyError, AttributeError) as e:
        logger.debug("yfinance history 30m для %s (start/end): %s", ticker, e)
        df = None
    df = _normalize(df)
    if df is not None:
        return _to_us_eastern(df)
    for period in ("60d", "30d", "7d", "5d", "2d", "1d"):
        try:
            df = t.history(period=period, interval="30m", auto_adjust=False)
            df = _normalize(df)
            if df is not None and not df.empty:
                logger.info("30m данные %s получены через period=%s", ticker, period)
                return _to_us_eastern(df)
        except Exception as e:
            logger.debug("yfinance period=%s 30m для %s: %s", period, ticker, e)
    logger.warning("Нет 30m данных для %s за %d дн.", ticker, days)
    return None


def has_5m_data(ticker: str, days: int = None, min_bars: int = 1) -> bool:
    """
    Есть ли 5m данные по тикеру: все доступные отметки от «сейчас» назад на days дней
    (по умолчанию 7 — полное окно Yahoo). Достаточно любого непустого набора баров.
    """
    if days is None:
        days = MAX_DAYS_5M
    df = fetch_5m_ohlc(ticker, days=days)
    return df is not None and not df.empty and len(df) >= min_bars


def fetch_kb_news_for_period(
    ticker: str,
    days: int,
    *,
    as_of: datetime | None = None,
    engine: Any = None,
) -> List[Dict[str, Any]]:
    """
    Новости/события из KB за те же days дней, что и окно 5m. Позволяет агенту
    сопоставить динамику цены и новости за один период и учесть влияние при решении.

    as_of: UTC naive cutoff for offline replay (only news with ingested_at/ts <= as_of).
    """
    try:
        from sqlalchemy import create_engine, text
        from config_loader import get_database_url
        end = as_of if as_of is not None else datetime.utcnow()
        cutoff = end - timedelta(days=days)
        eng = engine
        if eng is None:
            eng = create_engine(get_database_url())
            own_engine = True
        else:
            own_engine = False
        with eng.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT ts, ticker, source, content, sentiment_score, insight
                    FROM knowledge_base
                    WHERE (ticker = :ticker OR ticker IN ('MACRO', 'US_MACRO'))
                      AND COALESCE(ingested_at, ts) >= :cutoff
                      AND COALESCE(ingested_at, ts) <= :as_of_end
                      AND content IS NOT NULL
                      AND LENGTH(TRIM(content)) > 0
                    ORDER BY COALESCE(ingested_at, ts) DESC
                """),
                {"ticker": ticker, "cutoff": cutoff, "as_of_end": end},
            )
            rows = result.fetchall()
        out = []
        for row in rows:
            ts, kb_ticker, source, content, sentiment_score, insight = row
            content_str = (content or "")[:KB_NEWS_CONTENT_MAX_LEN]
            if len(content or "") > KB_NEWS_CONTENT_MAX_LEN:
                content_str += "..."
            out.append({
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "ticker": kb_ticker,
                "source": source or "",
                "content": content_str,
                "sentiment_score": float(sentiment_score) if sentiment_score is not None else None,
                "insight": (insight or "")[:300] if insight else None,
            })
        return out
    except Exception as e:
        logger.debug("KB новости за период %s %d дн.: %s", ticker, days, e)
        return []


def _log_returns(series: pd.Series) -> pd.Series:
    """Лог-доходности по правилам проекта."""
    return np.log(series / series.shift(1)).dropna()


def compute_5m_features(df: pd.DataFrame, ticker: str = "") -> Optional[Dict[str, Any]]:
    """
    Вычисляет все технические параметры по 5m-датафрейму один раз.
    Результат переиспользуется: для правил решения, context_json, промпта LLM и (при необходимости) для ML-модели.
    Не включает: загрузку KB-новостей, контекст сессии, итоговое решение (decision/reasoning).
    """
    if df is None or df.empty or "Close" not in df.columns:
        return None
    df = df.sort_values("datetime").reset_index(drop=True)
    closes = df["Close"].astype(float)
    high_5d = float(df["High"].max())
    low_5d = float(df["Low"].min())
    price = float(closes.iloc[-1])
    last_bar_high = float(df["High"].iloc[-1])
    last_bar_low = float(df["Low"].iloc[-1])
    n_tail = min(6, len(df))
    recent_bars_high_max = float(df["High"].iloc[-n_tail:].max()) if n_tail else last_bar_high
    recent_bars_low_min = float(df["Low"].iloc[-n_tail:].min()) if n_tail else last_bar_low

    # Время бара(ов), на котором достигнут max/min в «хвосте» последних N 5m — для диагностики тейка/стопа в кроне.
    recent_bars_high_ts = None
    recent_bars_low_ts = None
    try:
        tail = df.iloc[-n_tail:]
        hi_idx = int(tail["High"].astype(float).idxmax())
        lo_idx = int(tail["Low"].astype(float).idxmin())
        dt_hi = df.loc[hi_idx, "datetime"]
        dt_lo = df.loc[lo_idx, "datetime"]
        dts_hi = pd.to_datetime(dt_hi)
        dts_lo = pd.to_datetime(dt_lo)
        if dts_hi.tzinfo is None:
            dts_hi = dts_hi.tz_localize("America/New_York", ambiguous="infer")
        else:
            dts_hi = dts_hi.tz_convert("America/New_York")
        if dts_lo.tzinfo is None:
            dts_lo = dts_lo.tz_localize("America/New_York", ambiguous="infer")
        else:
            dts_lo = dts_lo.tz_convert("America/New_York")
        recent_bars_high_ts = dts_hi.isoformat()
        recent_bars_low_ts = dts_lo.isoformat()
    except Exception:
        pass

    session_high = high_5d
    session_high_ts = None
    try:
        dts = pd.to_datetime(df["datetime"])
        last_date = dts.max().date()
        session_mask = dts.dt.date == last_date
        session_high_val = float(df.loc[session_mask, "High"].max())
        try:
            sh_idx = int(df.loc[session_mask, "High"].astype(float).idxmax())
            dt_sh = df.loc[sh_idx, "datetime"]
            dts_sh = pd.to_datetime(dt_sh)
            if dts_sh.tzinfo is None:
                dts_sh = dts_sh.tz_localize("America/New_York", ambiguous="infer")
            else:
                dts_sh = dts_sh.tz_convert("America/New_York")
            session_high_ts = dts_sh.isoformat()
        except Exception:
            session_high_ts = None
    except Exception:
        session_high_val = high_5d
    if session_high_val > 0 and price < session_high_val:
        session_high = session_high_val

    curvature_5m_pct = None
    if len(closes) >= 3 and price > 0:
        d1 = float(closes.iloc[-1] - closes.iloc[-2])
        d0 = float(closes.iloc[-2] - closes.iloc[-3])
        curvature_5m_pct = (d1 - d0) / price * 100.0

    possible_bounce_to_high_pct = None
    estimated_bounce_pct = None
    if session_high > 0 and price > 0 and session_high >= price:
        possible_bounce_to_high_pct = (session_high - price) / price * 100.0
    if session_high > 0 and recent_bars_low_min > 0 and price > 0 and curvature_5m_pct is not None and curvature_5m_pct > 0:
        estimated_bounce_pct = 0.5 * (session_high - recent_bars_low_min) / price * 100.0

    log_ret = _log_returns(closes)
    volatility_5m_pct = float(log_ret.std() * 100) if len(log_ret) > 1 else 0.0
    rsi_5m = compute_rsi_5m(closes, period=RSI_PERIOD_5M)

    # Импульс: окно до 2ч (24 бара 5m), но при малом числе баров — сколько есть (на 15 мин торгов = 3 бара → импульс за 10 мин)
    n = min(BARS_2H, len(closes) - 1)
    momentum_2h_pct = 0.0
    momentum_window_min = 0
    if n >= 1 and len(closes) >= n + 1:
        price_2h_ago = float(closes.iloc[-(n + 1)])
        if price_2h_ago > 0:
            momentum_2h_pct = ((price / price_2h_ago) - 1.0) * 100.0
        momentum_window_min = n * 5  # 5m бары → минуты

    # Импульс только внутри последней календарной сессии RTH (ET) — не смешивать вчерашний подъём с сегодняшним открытием.
    # Используется для импульсного BUY; иначе в первые минуты дня срабатывает «рост за 24 бара» = вчера + гэп.
    momentum_rth_today_pct: Optional[float] = None
    momentum_rth_today_window_min = 0
    momentum_rth_today_bars = 0
    session_open_price: Optional[float] = None
    session_move_from_open_pct: Optional[float] = None
    bars_since_session_high: Optional[int] = None
    momentum_short_pct: Optional[float] = None
    momentum_short_bars = 0
    try:
        from datetime import time as dt_time

        dts_et = pd.to_datetime(df["datetime"])
        if dts_et.dt.tz is None:
            try:
                dts_et = dts_et.dt.tz_localize("America/New_York", ambiguous=True)
            except Exception:
                dts_et = dts_et.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
        else:
            dts_et = dts_et.dt.tz_convert("America/New_York")
        last_cal = dts_et.max().date()
        t_start = dt_time(*US_SESSION_START)
        t_end = dt_time(*US_SESSION_END)
        rth_mask = (dts_et.dt.date == last_cal) & (dts_et.dt.time >= t_start) & (dts_et.dt.time <= t_end)
        df_td = df.loc[rth_mask].sort_values("datetime")
        closes_td = df_td["Close"].astype(float).reset_index(drop=True)
        nt = len(closes_td)
        if nt >= 2:
            momentum_rth_today_bars = nt
            n_sess = min(BARS_2H, nt - 1)
            if n_sess >= 1 and nt >= n_sess + 1:
                p_open_ref = float(closes_td.iloc[-(n_sess + 1)])
                p_now = float(closes_td.iloc[-1])
                if p_open_ref > 0:
                    momentum_rth_today_pct = ((p_now / p_open_ref) - 1.0) * 100.0
                    momentum_rth_today_window_min = n_sess * 5
            if nt >= 1:
                if "Open" in df_td.columns:
                    session_open_price = float(df_td["Open"].iloc[0])
                else:
                    session_open_price = float(closes_td.iloc[0])
                if session_open_price and session_open_price > 0:
                    session_move_from_open_pct = ((price / session_open_price) - 1.0) * 100.0
                if "High" in df_td.columns:
                    highs_td = df_td["High"].astype(float).reset_index(drop=True)
                    sh_td = float(highs_td.max())
                    tol = max(sh_td * 1e-6, 0.02)
                    last_high_idx = None
                    for j in range(nt - 1, -1, -1):
                        if float(highs_td.iloc[j]) >= sh_td - tol:
                            last_high_idx = j
                            break
                    if last_high_idx is not None:
                        bars_since_session_high = max(0, nt - 1 - last_high_idx)
    except Exception as e:
        logger.debug("momentum_rth_today %s: %s", ticker, e)

    try:
        from config_loader import get_config_value as _gcv_short

        n_short = min(
            max(1, int((_gcv_short("GAME_5M_ENTRY_SHORT_MOMENTUM_BARS", "6") or "6").strip())),
            len(closes) - 1,
        )
        if n_short >= 1 and len(closes) >= n_short + 1:
            p_short_ref = float(closes.iloc[-(n_short + 1)])
            if p_short_ref > 0:
                momentum_short_pct = ((price / p_short_ref) - 1.0) * 100.0
                momentum_short_bars = n_short
    except Exception as e:
        logger.debug("momentum_short %s: %s", ticker, e)

    dt_min, dt_max = df["datetime"].min(), df["datetime"].max()
    period_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}" if hasattr(dt_min, "strftime") else f"{dt_min} – {dt_max}"

    pullback_from_high_pct = (session_high - price) / session_high * 100.0 if session_high > 0 and price < session_high else 0.0

    # ATR по 5m (14 баров): среднее от True Range, в % от цены
    atr_5m = None
    if len(df) >= 2 and all(c in df.columns for c in ("High", "Low", "Close")):
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        tr = np.maximum(high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
        tail = tr.iloc[-14:] if len(tr) >= 14 else tr
        mean_tr = tail.replace([np.inf, -np.inf], np.nan).dropna().mean()
        if pd.notna(mean_tr) and mean_tr > 0 and price > 0:
            atr_5m = round(float(mean_tr) / price * 100.0, 4)

    # Объём: последний бар vs среднее за хвост (если есть Volume)
    volume_5m_last = None
    volume_vs_avg_pct = None
    if "Volume" in df.columns:
        vol = df["Volume"].replace(0, np.nan).dropna()
        if len(vol) > 0:
            volume_5m_last = int(vol.iloc[-1])
            tail = min(20, len(vol))
            avg_vol = float(vol.iloc[-tail:].mean())
            if avg_vol > 0 and volume_5m_last is not None:
                volume_vs_avg_pct = round(volume_5m_last / avg_vol * 100.0, 2)

    return {
        "price": price,
        "high_5d": high_5d,
        "low_5d": low_5d,
        "rsi_5m": rsi_5m,
        "volatility_5m_pct": volatility_5m_pct,
        "momentum_2h_pct": momentum_2h_pct,
        "momentum_window_min": momentum_window_min,  # фактическое окно импульса (мин), для подписи «импульс за N мин»
        "momentum_rth_today_pct": momentum_rth_today_pct,
        "momentum_rth_today_window_min": momentum_rth_today_window_min,
        "momentum_rth_today_bars": momentum_rth_today_bars,
        "session_open_price": session_open_price,
        "session_move_from_open_pct": session_move_from_open_pct,
        "bars_since_session_high": bars_since_session_high,
        "momentum_short_pct": momentum_short_pct,
        "momentum_short_bars": momentum_short_bars,
        "session_high": session_high,
        "session_high_ts": session_high_ts,
        "pullback_from_high_pct": pullback_from_high_pct,
        "last_bar_high": last_bar_high,
        "last_bar_low": last_bar_low,
        "recent_bars_high_max": recent_bars_high_max,
        "recent_bars_high_ts": recent_bars_high_ts,
        "recent_bars_low_min": recent_bars_low_min,
        "recent_bars_low_ts": recent_bars_low_ts,
        "curvature_5m_pct": curvature_5m_pct,
        "possible_bounce_to_high_pct": possible_bounce_to_high_pct,
        "estimated_bounce_pct": estimated_bounce_pct,
        "period_str": period_str,
        "bars_count": len(df),
        "atr_5m_pct": atr_5m,
        "volume_5m_last": volume_5m_last,
        "volume_vs_avg_pct": volume_vs_avg_pct,
    }


def compute_rsi_5m(closes: pd.Series, period: int = RSI_PERIOD_5M) -> Optional[float]:
    """RSI по ряду 5m закрытий (последнее значение = текущее)."""
    from services.rsi_calculator import compute_rsi_from_closes
    vals = closes.dropna().tolist()
    if len(vals) < period + 1:
        return None
    return compute_rsi_from_closes(vals, period=period)


def compute_30m_features(df: pd.DataFrame, ticker: str = "") -> Optional[Dict[str, Any]]:
    """
    Технические признаки по 30m OHLC (те же ключи, что у compute_5m_features, для общих правил GAME_5M).
    Импульс «2ч» = 4×30m; окно RTH-сессии — до 4 баров 30m назад; ATR/волатильность по 30m ряду.
    """
    if df is None or df.empty or "Close" not in df.columns:
        return None
    df = df.sort_values("datetime").reset_index(drop=True)
    closes = df["Close"].astype(float)
    high_5d = float(df["High"].max())
    low_5d = float(df["Low"].min())
    price = float(closes.iloc[-1])
    last_bar_high = float(df["High"].iloc[-1])
    last_bar_low = float(df["Low"].iloc[-1])
    n_tail = min(6, len(df))
    recent_bars_high_max = float(df["High"].iloc[-n_tail:].max()) if n_tail else last_bar_high
    recent_bars_low_min = float(df["Low"].iloc[-n_tail:].min()) if n_tail else last_bar_low

    recent_bars_high_ts = None
    recent_bars_low_ts = None
    try:
        tail = df.iloc[-n_tail:]
        hi_idx = int(tail["High"].astype(float).idxmax())
        lo_idx = int(tail["Low"].astype(float).idxmin())
        dt_hi = df.loc[hi_idx, "datetime"]
        dt_lo = df.loc[lo_idx, "datetime"]
        dts_hi = pd.to_datetime(dt_hi)
        dts_lo = pd.to_datetime(dt_lo)
        if dts_hi.tzinfo is None:
            dts_hi = dts_hi.tz_localize("America/New_York", ambiguous="infer")
        else:
            dts_hi = dts_hi.tz_convert("America/New_York")
        if dts_lo.tzinfo is None:
            dts_lo = dts_lo.tz_localize("America/New_York", ambiguous="infer")
        else:
            dts_lo = dts_lo.tz_convert("America/New_York")
        recent_bars_high_ts = dts_hi.isoformat()
        recent_bars_low_ts = dts_lo.isoformat()
    except Exception:
        pass

    session_high = high_5d
    session_high_ts = None
    try:
        dts = pd.to_datetime(df["datetime"])
        last_date = dts.max().date()
        session_mask = dts.dt.date == last_date
        session_high_val = float(df.loc[session_mask, "High"].max())
        try:
            sh_idx = int(df.loc[session_mask, "High"].astype(float).idxmax())
            dt_sh = df.loc[sh_idx, "datetime"]
            dts_sh = pd.to_datetime(dt_sh)
            if dts_sh.tzinfo is None:
                dts_sh = dts_sh.tz_localize("America/New_York", ambiguous="infer")
            else:
                dts_sh = dts_sh.tz_convert("America/New_York")
            session_high_ts = dts_sh.isoformat()
        except Exception:
            session_high_ts = None
    except Exception:
        session_high_val = high_5d
    if session_high_val > 0 and price < session_high_val:
        session_high = session_high_val

    curvature_5m_pct = None
    if len(closes) >= 3 and price > 0:
        d1 = float(closes.iloc[-1] - closes.iloc[-2])
        d0 = float(closes.iloc[-2] - closes.iloc[-3])
        curvature_5m_pct = (d1 - d0) / price * 100.0

    possible_bounce_to_high_pct = None
    estimated_bounce_pct = None
    if session_high > 0 and price > 0 and session_high >= price:
        possible_bounce_to_high_pct = (session_high - price) / price * 100.0
    if session_high > 0 and recent_bars_low_min > 0 and price > 0 and curvature_5m_pct is not None and curvature_5m_pct > 0:
        estimated_bounce_pct = 0.5 * (session_high - recent_bars_low_min) / price * 100.0

    log_ret = _log_returns(closes)
    volatility_5m_pct = float(log_ret.std() * 100) if len(log_ret) > 1 else 0.0
    rsi_5m = compute_rsi_5m(closes, period=RSI_PERIOD_5M)

    bar_minutes = 30
    n = min(BARS_2H_30M, len(closes) - 1)
    momentum_2h_pct = 0.0
    momentum_window_min = 0
    if n >= 1 and len(closes) >= n + 1:
        price_2h_ago = float(closes.iloc[-(n + 1)])
        if price_2h_ago > 0:
            momentum_2h_pct = ((price / price_2h_ago) - 1.0) * 100.0
        momentum_window_min = n * bar_minutes

    momentum_rth_today_pct: Optional[float] = None
    momentum_rth_today_window_min = 0
    momentum_rth_today_bars = 0
    try:
        from datetime import time as dt_time

        dts_et = pd.to_datetime(df["datetime"])
        if dts_et.dt.tz is None:
            try:
                dts_et = dts_et.dt.tz_localize("America/New_York", ambiguous=True)
            except Exception:
                dts_et = dts_et.dt.tz_localize("UTC", ambiguous=True).dt.tz_convert("America/New_York")
        else:
            dts_et = dts_et.dt.tz_convert("America/New_York")
        last_cal = dts_et.max().date()
        t_start = dt_time(*US_SESSION_START)
        t_end = dt_time(*US_SESSION_END)
        rth_mask = (dts_et.dt.date == last_cal) & (dts_et.dt.time >= t_start) & (dts_et.dt.time <= t_end)
        df_td = df.loc[rth_mask].sort_values("datetime")
        closes_td = df_td["Close"].astype(float).reset_index(drop=True)
        nt = len(closes_td)
        if nt >= 2:
            momentum_rth_today_bars = nt
            n_sess = min(BARS_2H_30M, nt - 1)
            if n_sess >= 1 and nt >= n_sess + 1:
                p_open_ref = float(closes_td.iloc[-(n_sess + 1)])
                p_now = float(closes_td.iloc[-1])
                if p_open_ref > 0:
                    momentum_rth_today_pct = ((p_now / p_open_ref) - 1.0) * 100.0
                    momentum_rth_today_window_min = n_sess * bar_minutes
    except Exception as e:
        logger.debug("momentum_rth_today 30m %s: %s", ticker, e)

    dt_min, dt_max = df["datetime"].min(), df["datetime"].max()
    period_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}" if hasattr(dt_min, "strftime") else f"{dt_min} – {dt_max}"

    pullback_from_high_pct = (session_high - price) / session_high * 100.0 if session_high > 0 and price < session_high else 0.0

    atr_5m = None
    if len(df) >= 2 and all(c in df.columns for c in ("High", "Low", "Close")):
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        tr = np.maximum(high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
        tail = tr.iloc[-14:] if len(tr) >= 14 else tr
        mean_tr = tail.replace([np.inf, -np.inf], np.nan).dropna().mean()
        if pd.notna(mean_tr) and mean_tr > 0 and price > 0:
            atr_5m = round(float(mean_tr) / price * 100.0, 4)

    volume_5m_last = None
    volume_vs_avg_pct = None
    if "Volume" in df.columns:
        vol = df["Volume"].replace(0, np.nan).dropna()
        if len(vol) > 0:
            volume_5m_last = int(vol.iloc[-1])
            tail_v = min(20, len(vol))
            avg_vol = float(vol.iloc[-tail_v:].mean())
            if avg_vol > 0 and volume_5m_last is not None:
                volume_vs_avg_pct = round(volume_5m_last / avg_vol * 100.0, 2)

    return {
        "price": price,
        "high_5d": high_5d,
        "low_5d": low_5d,
        "rsi_5m": rsi_5m,
        "volatility_5m_pct": volatility_5m_pct,
        "momentum_2h_pct": momentum_2h_pct,
        "momentum_window_min": momentum_window_min,
        "momentum_rth_today_pct": momentum_rth_today_pct,
        "momentum_rth_today_window_min": momentum_rth_today_window_min,
        "momentum_rth_today_bars": momentum_rth_today_bars,
        "session_high": session_high,
        "session_high_ts": session_high_ts,
        "pullback_from_high_pct": pullback_from_high_pct,
        "last_bar_high": last_bar_high,
        "last_bar_low": last_bar_low,
        "recent_bars_high_max": recent_bars_high_max,
        "recent_bars_high_ts": recent_bars_high_ts,
        "recent_bars_low_min": recent_bars_low_min,
        "recent_bars_low_ts": recent_bars_low_ts,
        "curvature_5m_pct": curvature_5m_pct,
        "possible_bounce_to_high_pct": possible_bounce_to_high_pct,
        "estimated_bounce_pct": estimated_bounce_pct,
        "period_str": period_str,
        "bars_count": len(df),
        "atr_5m_pct": atr_5m,
        "volume_5m_last": volume_5m_last,
        "volume_vs_avg_pct": volume_vs_avg_pct,
        "timeframe": "30m",
    }


def _entry_stale_chase_guard_params() -> Dict[str, Any]:
    from config_loader import get_config_value as _gcv

    enabled = (_gcv("GAME_5M_ENTRY_STALE_CHASE_GUARD_ENABLED", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        extended_move_pct = float((_gcv("GAME_5M_ENTRY_EXTENDED_SESSION_MOVE_PCT", "2.5") or "2.5").strip())
    except (TypeError, ValueError):
        extended_move_pct = 2.5
    try:
        max_pullback_pct = float((_gcv("GAME_5M_ENTRY_CHASE_MAX_PULLBACK_FROM_HIGH_PCT", "2.0") or "2.0").strip())
    except (TypeError, ValueError):
        max_pullback_pct = 2.0
    try:
        min_bars_since_high = int(float((_gcv("GAME_5M_ENTRY_CHASE_MIN_BARS_SINCE_HIGH", "6") or "6").strip()))
    except (TypeError, ValueError):
        min_bars_since_high = 6
    return {
        "enabled": enabled,
        "extended_move_pct": extended_move_pct,
        "max_pullback_pct": max_pullback_pct,
        "min_bars_since_high": max(1, min(min_bars_since_high, 48)),
    }


def _rth_intraday_entry_active(
    session_phase: Optional[str],
    mom_rth_bars: int,
    min_session_bars: int,
) -> bool:
    """True when today's RTH has enough 5m bars for live intraday momentum entries."""
    phase = (session_phase or "").strip().upper()
    if phase in ("PRE_MARKET", "AFTER_HOURS", "WEEKEND", "HOLIDAY"):
        return False
    return int(mom_rth_bars) >= int(min_session_bars)


def apply_entry_stale_chase_guard(
    decision: str,
    reasons: List[str],
    features: Dict[str, Any],
    *,
    rth_intraday_active: bool = True,
) -> Tuple[str, List[str], bool, Optional[str], Optional[str]]:
    """
    BUY/STRONG_BUY → HOLD, если импульс «протух»: сессия уже уехала, цена у day-high,
    новый session high давно не обновлялся (chase, не свежий вход).
    """
    params = _entry_stale_chase_guard_params()
    if not params["enabled"] or decision not in ("BUY", "STRONG_BUY") or not rth_intraday_active:
        return decision, reasons, False, None, None

    session_move = features.get("session_move_from_open_pct")
    pullback = features.get("pullback_from_high_pct")
    bars_since = features.get("bars_since_session_high")
    if session_move is None or pullback is None or bars_since is None:
        return decision, reasons, False, None, None

    stale_chase = (
        float(session_move) >= float(params["extended_move_pct"])
        and float(pullback) <= float(params["max_pullback_pct"])
        and int(bars_since) >= int(params["min_bars_since_high"])
    )
    if not stale_chase:
        return decision, reasons, False, None, None

    prev = decision
    decision = "HOLD"
    guard_msg = (
        "entry stale/chase guard: сессия "
        f"+{float(session_move):.1f}%, у хая (откат {float(pullback):.2f}%), "
        f"без нового high {int(bars_since)} бар (~{int(bars_since) * 5}м) — сигнал входа протух"
    )
    reasons.append(guard_msg)
    return decision, reasons, True, prev, guard_msg


def decide_game5m_technical(
    *,
    ticker: str,
    features: Dict[str, Any],
    closes: pd.Series,
    th: Dict[str, Any],
    rsi_prev_values: List[float],
    decision_rule_params: Dict[str, Any],
    min_session_bars: int,
    premarket_intraday_momentum_pct: Optional[float],
    early_use_premarket_mom: bool,
    session_phase: Optional[str] = None,
) -> Tuple[str, List[str], Optional[str], bool]:
    """
    Техническое ядро BUY/HOLD/SELL GAME_5M (без KB, VIX, премаркет-гейта сессии, price_forecast).
    min_session_bars — число баров **текущего** таймфрейма (5m в кроне; для 30m передать сконвертированный порог).
    """
    from config_loader import get_config_value as _gcv

    vol_wait_min = float(th["volatility_wait_min"])
    sell_confirm_bars = int(th["sell_confirm_bars"])
    pm_mom_buy_min = float(th["premarket_momentum_buy_min"])
    pm_mom_block_below = float(th["premarket_momentum_block_below"])
    rsi_strong_buy_max = float(th["rsi_strong_buy_max"])
    momentum_for_strong_buy_min = float(th["momentum_for_strong_buy_min"])
    rsi_buy_max = float(th["rsi_buy_max"])
    price_to_low5d_multiplier_max = float(th["price_to_low5d_multiplier_max"])
    rsi_sell_min = float(th["rsi_sell_min"])
    rsi_hold_overbought_min = float(th["rsi_hold_overbought_min"])
    rth_momentum_buy_min = float(th["momentum_buy_min"])
    rsi_for_momentum_buy_max = float(th["rsi_for_momentum_buy_max"])
    volatility_warn_buy_min = float(th["volatility_warn_buy_min"])

    allow_cross_day_mom_buy = (_gcv("GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    price = features["price"]
    low_5d = features["low_5d"]
    rsi_5m = features["rsi_5m"]
    volatility_5m_pct = features["volatility_5m_pct"]
    momentum_2h_pct = features["momentum_2h_pct"]
    mom_rth_pct = features.get("momentum_rth_today_pct")
    mom_rth_bars = int(features.get("momentum_rth_today_bars") or 0)
    rth_intraday_active = _rth_intraday_entry_active(session_phase, mom_rth_bars, min_session_bars)
    session_move = features.get("session_move_from_open_pct")
    bars_since_high = features.get("bars_since_session_high")
    stale_params = _entry_stale_chase_guard_params()
    extended_move_pct = float(stale_params["extended_move_pct"])
    min_bars_stale = int(stale_params["min_bars_since_high"])
    block_stale_strong = (
        session_move is not None
        and float(session_move) >= extended_move_pct
        and bars_since_high is not None
        and int(bars_since_high) >= min_bars_stale
    )

    decision = "HOLD"
    reasons: List[str] = []
    technical_entry_branch: Optional[str] = None
    entry_strong_buy_downgraded = False

    if rsi_5m is not None:
        if (
            rth_intraday_active
            and rsi_5m <= rsi_strong_buy_max
            and momentum_2h_pct >= momentum_for_strong_buy_min
            and not block_stale_strong
        ):
            decision = "STRONG_BUY"
            technical_entry_branch = "strong_buy_rsi"
            reasons.append(f"RSI(5m)={rsi_5m:.1f} — перепроданность, отскок")
        elif rsi_5m <= rsi_buy_max and price <= low_5d * price_to_low5d_multiplier_max:
            decision = "BUY"
            technical_entry_branch = "buy_5d_low"
            reasons.append(f"RSI(5m)={rsi_5m:.1f}, цена у 5д минимума")
        elif rsi_5m >= rsi_sell_min:
            sell_confirmed = (
                len(rsi_prev_values) >= sell_confirm_bars
                and all(v >= rsi_sell_min for v in rsi_prev_values[:sell_confirm_bars])
            )
            if sell_confirmed:
                decision = "SELL"
                reasons.append(f"RSI(5m)={rsi_5m:.1f} и подтверждение {sell_confirm_bars} баров — перекупленность")
            else:
                reasons.append(f"RSI(5m)={rsi_5m:.1f} — перекупленность без подтверждения {sell_confirm_bars} баров, ждём")
        elif rsi_5m >= rsi_hold_overbought_min:
            if decision == "HOLD":
                reasons.append(f"RSI(5m)={rsi_5m:.1f} — ближе к перекупленности, ждать")
        elif (
            rth_intraday_active
            and mom_rth_pct is not None
            and mom_rth_bars >= min_session_bars
            and (rsi_5m is None or rsi_5m < rsi_for_momentum_buy_max)
        ):
            mom_for_entry = mom_rth_pct
            if (
                session_move is not None
                and float(session_move) >= extended_move_pct
                and features.get("momentum_short_pct") is not None
            ):
                mom_for_entry = features.get("momentum_short_pct")
            if decision == "HOLD" and float(mom_for_entry) > rth_momentum_buy_min:
                wmin = int(features.get("momentum_rth_today_window_min") or 0)
                short_bars = int(features.get("momentum_short_bars") or 0)
                decision = "BUY"
                technical_entry_branch = "buy_rth_momentum"
                if (
                    session_move is not None
                    and float(session_move) >= extended_move_pct
                    and features.get("momentum_short_pct") is not None
                ):
                    reasons.append(
                        f"импульс +{float(mom_for_entry):.2f}% за ~{short_bars * 5} мин (сессия +{float(session_move):.2f}%), RSI не перекуплен"
                    )
                else:
                    reasons.append(
                        f"импульс +{float(mom_for_entry):.2f}% за текущую сессию RTH (~{wmin} мин), RSI не перекуплен"
                    )
        elif (
            early_use_premarket_mom
            and mom_rth_bars < min_session_bars
            and premarket_intraday_momentum_pct is not None
            and float(premarket_intraday_momentum_pct) >= pm_mom_block_below
            and float(premarket_intraday_momentum_pct) > pm_mom_buy_min
            and (rsi_5m is None or rsi_5m < rsi_for_momentum_buy_max)
        ):
            if decision == "HOLD":
                decision = "BUY"
                technical_entry_branch = "buy_premarket_momentum"
                reasons.append(
                    f"импульс премаркет +{float(premarket_intraday_momentum_pct):.2f}% "
                    f"(1m до 9:30 ET; ранний RTH, баров сессии {mom_rth_bars} < {min_session_bars}), RSI не перекуплен"
                )
        elif (
            rth_intraday_active
            and allow_cross_day_mom_buy
            and momentum_2h_pct > rth_momentum_buy_min
            and (rsi_5m is None or rsi_5m < rsi_for_momentum_buy_max)
        ):
            if decision == "HOLD":
                decision = "BUY"
                technical_entry_branch = "buy_cross_day_2h"
                reasons.append(
                    f"импульс +{momentum_2h_pct:.2f}% за окно до 2ч (кросс-дневной; GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY), RSI не перекуплен"
                )

    if volatility_5m_pct > volatility_warn_buy_min and decision in ("BUY", "STRONG_BUY"):
        reasons.append(f"волатильность 5m высокая ({volatility_5m_pct:.2f}%) — предпочтительны лимитные ордера")
    elif volatility_5m_pct > vol_wait_min:
        if decision == "HOLD":
            reasons.append(f"волатильность 5m {volatility_5m_pct:.2f}% > порога {vol_wait_min:.2f}% — выжидать")

    atr_5m_pct = features.get("atr_5m_pct")
    volume_vs_avg_pct = features.get("volume_vs_avg_pct")
    _min_vol = _gcv("GAME_5M_MIN_VOLUME_VS_AVG_PCT", "").strip()
    _max_atr = _gcv("GAME_5M_MAX_ATR_5M_PCT", "").strip()
    decision_rule_params["cfg_min_volume_vs_avg_pct"] = _min_vol or None
    decision_rule_params["cfg_max_atr_5m_pct"] = _max_atr or None
    if _min_vol and volume_vs_avg_pct is not None and decision in ("BUY", "STRONG_BUY"):
        try:
            min_vol = float(_min_vol)
            if volume_vs_avg_pct < min_vol:
                decision = "HOLD"
                reasons.append(f"объём {volume_vs_avg_pct:.0f}% от среднего < порога {min_vol:.0f}% — вход отложен")
        except (ValueError, TypeError):
            pass
    if _max_atr and atr_5m_pct is not None and decision in ("BUY", "STRONG_BUY"):
        try:
            max_atr = float(_max_atr)
            if atr_5m_pct > max_atr:
                decision = "HOLD"
                reasons.append(f"ATR 5m {atr_5m_pct:.2f}% > порога {max_atr}% — высокая волатильность, вход отложен")
        except (ValueError, TypeError):
            pass

    return decision, reasons, technical_entry_branch, entry_strong_buy_downgraded


# Минимум баров 1m премаркета для расчёта признаков (RSI 14 + импульс ~30 мин)
PREMARKET_MIN_BARS = 14
# Баров для «импульса» в премаркете (≈30 мин при 1m)
PREMARKET_MOMENTUM_BARS = 30


def compute_premarket_features(df_1m: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Признаки по 1m барам премаркета (Yahoo prepost).
    Используется в PRE_MARKET для расчёта RSI, волатильности, импульса и далее upside/downside/prob.
    Возвращает словарь с полями, совместимыми с 5m (rsi_5m, volatility_5m_pct, momentum_2h_pct, period_str, ...).
    """
    if df_1m is None or df_1m.empty or "Close" not in df_1m.columns:
        return None
    dt_col = "Datetime" if "Datetime" in df_1m.columns else "datetime"
    if dt_col not in df_1m.columns and "Date" in df_1m.columns:
        dt_col = "Date"
    if dt_col not in df_1m.columns:
        return None
    df = df_1m.sort_values(dt_col).reset_index(drop=True)
    closes = df["Close"].astype(float)
    price = float(closes.iloc[-1])
    if price <= 0:
        return None
    n = len(closes)
    if n < PREMARKET_MIN_BARS:
        return None

    rsi = compute_rsi_5m(closes, period=14)
    log_ret = _log_returns(closes)
    volatility_pct = float(log_ret.std() * 100) if len(log_ret) > 1 else 0.0

    mom_bars = min(PREMARKET_MOMENTUM_BARS, n - 1)
    momentum_pct = 0.0
    if mom_bars >= 1 and n >= mom_bars + 1:
        price_ago = float(closes.iloc[-(mom_bars + 1)])
        if price_ago > 0:
            momentum_pct = ((price / price_ago) - 1.0) * 100.0

    n_tail = min(10, n)
    recent_high = float(df["High"].iloc[-n_tail:].max()) if "High" in df.columns else price
    recent_low = float(df["Low"].iloc[-n_tail:].min()) if "Low" in df.columns else price

    try:
        dt_min = df[dt_col].iloc[0]
        dt_max = df[dt_col].iloc[-1]
        if hasattr(dt_min, "strftime"):
            period_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')} (премаркет 1m)"
        else:
            period_str = "премаркет 1m (предварительно)"
    except Exception:
        period_str = "премаркет 1m (предварительно)"

    return {
        "price": price,
        "rsi_5m": round(rsi, 2) if rsi is not None else None,
        "volatility_5m_pct": round(volatility_pct, 4),
        "momentum_2h_pct": round(momentum_pct, 2),
        "period_str": period_str,
        "recent_bars_high_max": recent_high,
        "recent_bars_low_min": recent_low,
        "bars_count": n,
    }


# Поля технического сигнала 5m: один источник правды для signal5m, recommend5m, веб-карточек и cron.
# Все параметры считаются только в get_decision_5m(); здесь — список ключей для единого payload.
TECHNICAL_SIGNAL_KEYS = (
    "decision", "reasoning", "price", "rsi_5m", "momentum_2h_pct", "momentum_window_min",
    "momentum_rth_today_pct", "momentum_rth_today_window_min", "momentum_rth_today_bars",
    "premarket_intraday_momentum_pct",
    "volatility_5m_pct",
    "period_str", "bars_count", "stop_loss_pct", "take_profit_pct", "stop_loss_enabled",
    "estimated_upside_pct_day",
    # Апсайд по смеси 60/120m до применения min 4% (и до записи в estimated_upside_pct_day)
    "estimated_upside_forecast_raw_pct",
    "suggested_take_profit_price",
    "entry_price_recommended", "entry_price_range_low", "entry_price_range_high", "expected_profit_pct_if_take",
    "estimated_downside_pct_day", "prob_up", "prob_down",
    "pullback_from_high_pct", "session_high",
    "kb_news_impact", "entry_advice", "entry_advice_reason", "entry_advice_reason_local",
    "macro_risk_level", "macro_equity_gap_bias", "macro_risk_reasons", "macro_indicators",
    "ndx_gap_pct", "spy_gap_pct", "ndx_premarket_last", "ndx_prev_close",
    "macro_predicted_sector_gap_pct", "macro_sector_proxy",
    "market_session",
    "session_phase", "entry_price_basis",
    "open_guard_triggered", "open_guard_bar_range_pct", "open_guard_mins_since_open",
    "open_guard_prev_decision", "open_guard_next_decision",
    "premarket_gap_pct", "premarket_last", "prev_close", "minutes_until_open",
    "premarket_entry_recommendation", "premarket_suggested_limit_price",
    "rth_open_gap_pct", "rth_open_price",
    "ticker_open_gap_predicted_pct", "ticker_open_gap_predicted_source",
    "ticker_open_gap_ml_advisory_pct", "ticker_open_gap_ml_advisory_source",
    "ticker_open_gap_observable_baseline_pct",
    "ticker_open_gap_fact_pct", "ticker_open_gap_fact_basis", "ticker_open_gap_model_version",
    "ticker_open_gap_confidence", "ticker_open_gap_uncertainty_p80_pp", "ticker_open_gap_model_n_train",
    "forecast_layer", "forecast_open_gap_pct", "forecast_open_gap_fact_pct",
    "forecast_open_gap_baseline_pct", "forecast_open_gap_ml_advisory_pct",
    "forecast_open_gap_confidence", "forecast_open_gap_source", "forecast_open_gap_uncertainty_p80_pp",
    "forecast_horizons_pct", "forecast_regime", "forecast_ready",
    "forecast_gap_up_opportunity", "forecast_gap_up_should_boost_entry",
    "forecast_gap_up_opportunity_reason", "forecast_gap_up_opportunity_source",
    "is_preliminary",
    # CatBoost (опционально); итог для входа/LLM — technical_decision_effective
    "catboost_entry_proba_good", "catboost_signal_status", "catboost_signal_note",
    "catboost_entry_proba_good_v2", "catboost_bar_v2_signal_status", "catboost_bar_v2_signal_note",
        "catboost_entry_proba_good_e3", "entry_e3_signal_status", "entry_e3_signal_note",
        "ml_entry_e3", "ml_hold_h3", "has_open_position", "open_position_pnl_pct",
    "technical_decision_core", "technical_decision_effective",
    "catboost_fusion_mode", "catboost_fusion_note",
    "entry_quality_guard_triggered", "entry_quality_guard_reason", "entry_quality_guard_prev_decision",
    "entry_stale_chase_guard_triggered", "entry_stale_chase_guard_reason", "entry_stale_chase_guard_prev_decision",
    "session_open_price", "session_move_from_open_pct", "bars_since_session_high",
    "momentum_short_pct", "momentum_short_bars",
    # Явный разбор входа: формальное условие ветки + интуиция стратегии (не дублирует весь reasoning)
    "technical_entry_branch", "entry_strong_buy_downgraded", "entry_condition", "entry_intuition",
    # Прогноз цены на 30/60/120 мин (лог-нормаль по 5m лог-доходностям)
    "price_forecast_5m", "price_forecast_5m_summary",
    # Ridge на дневных лог-доходностях 1–3 торговых дня + контекст 5m (см. log_return_multiday_forecast)
    "log_return_multiday_forecast", "log_return_multiday_forecast_summary",
    # Плоские поля мультидневного ridge (% vs spot по торговым дням 1/2/3, для карточек/правил/контекста сделки)
    "multiday_lr_horizon_1d_pct_vs_spot", "multiday_lr_horizon_2d_pct_vs_spot", "multiday_lr_horizon_3d_pct_vs_spot",
    "multiday_lr_horizon_1d_train_rmse_log", "multiday_lr_horizon_2d_train_rmse_log", "multiday_lr_horizon_3d_train_rmse_log",
    "multiday_lr_bias", "multiday_lr_daily_last_date", "multiday_lr_method", "multiday_lr_premarket_db_used",
    "multiday_lr_news_db_used", "multiday_lr_macro_calendar_db_used", "multiday_lr_symbol_calendar_db_used",
    "multiday_lr_n_features",
    "multiday_lr_daily_close_source",
    "multiday_lr_forecast_unavailable",
    "multiday_lr_forecast_error",
    "multiday_lr_entry_gate_mode",
    "multiday_lr_entry_gate_status",
    "multiday_lr_entry_gate_would_hold",
    "multiday_lr_entry_gate_applied",
    "multiday_lr_entry_gate_note",
    # Unified decision stack / product verdict
    "decision_effective",
    "decision_stack_version",
    "decision_stack_projected_effective",
    "decision_snapshot",
    "decision_verdict",
)


def _decision_verdict_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    eff = str(snapshot.get("effective_decision") or snapshot.get("core_decision") or "HOLD").upper()
    projected = str(snapshot.get("projected_effective_if_resolve") or eff).upper()
    contributions = snapshot.get("contributions") if isinstance(snapshot.get("contributions"), list) else []
    blockers = [c for c in contributions if c.get("action") in ("veto", "downgrade")]
    cautions = [c for c in contributions if float(c.get("strength") or 0.0) < -0.2]
    positives = [c for c in contributions if float(c.get("strength") or 0.0) > 0.2]
    if eff in ("BUY", "STRONG_BUY"):
        label = "Trade"
    elif eff in ("SELL", "STRONG_SELL"):
        label = "Exit"
    elif projected == "HOLD" and blockers:
        label = "Avoid"
    elif cautions:
        label = "Wait"
    else:
        label = "Hold"

    reasons = []
    for c in (blockers or cautions or positives or contributions)[:3]:
        detail = str(c.get("detail") or "").strip()
        if detail:
            reasons.append(f"{c.get('contour_id')}: {detail}")
    return {
        "label": label,
        "effective_decision": eff,
        "projected_effective_if_resolve": projected,
        "resolve_divergence": bool(snapshot.get("resolve_divergence")),
        "primary_reasons": reasons,
    }


def get_5m_card_payload(d5: Dict[str, Any], ticker: str) -> Dict[str, Any]:
    """
    Единый payload для отображения 5m (веб-карточки, Telegram, отчёты).
    Все поля берутся из выхода get_decision_5m(); один источник правды.
    Дополнительно считаем параметры чек-листа Квена: риск/ревард и мат. ожидание (docs/GAME_5M_WEB_CARDS.md).
    """
    if not d5:
        return {"ticker": ticker, "decision": "NO_DATA", "reasoning": "Нет 5m данных."}
    out = {"ticker": ticker}
    for k in TECHNICAL_SIGNAL_KEYS:
        if k in d5:
            out[k] = d5[k]
    snap = d5.get("decision_snapshot")
    if isinstance(snap, dict) and "decision_verdict" not in out:
        out["decision_verdict"] = _decision_verdict_from_snapshot(snap)
    # Параметры чек-листа Квена (5 параметров решения): риск/ревард и мат. ожидание — выводятся в карточке отдельно
    upside = d5.get("estimated_upside_pct_day")
    downside = d5.get("estimated_downside_pct_day")
    prob_up = d5.get("prob_up")
    prob_down = d5.get("prob_down")
    if downside is not None and float(downside) > 0 and upside is not None:
        out["risk_reward_ratio"] = round(float(upside) / float(downside), 2)
    else:
        out["risk_reward_ratio"] = None
    if (
        prob_up is not None
        and prob_down is not None
        and upside is not None
        and downside is not None
    ):
        try:
            ev = float(prob_up) * float(upside) - float(prob_down) * float(downside)
            out["expected_value_pct"] = round(ev, 2)
        except (TypeError, ValueError):
            out["expected_value_pct"] = None
    else:
        out["expected_value_pct"] = None

    # Текстовый вывод Квена: почему параметры сделки позитивны или негативны (по чек-листу)
    rr = out.get("risk_reward_ratio")
    ev = out.get("expected_value_pct")
    rr_ok = rr is not None and rr >= 2.0  # риск/ревард ≥ 1:2
    ev_ok = ev is not None and ev > 0
    if rr is None and ev is None:
        out["qwen_checklist_verdict"] = "Нейтрально: нет данных по R:R и мат.ожиданию дохода."
    elif rr_ok and ev_ok:
        out["qwen_checklist_verdict"] = (
            f"Позитивно: R:R 1:{rr:.1f} (≥1:2), мат.ожидание дохода {ev:+.2f}% (>0)."
        )
    elif not rr_ok and not ev_ok:
        parts = []
        if rr is not None and rr < 2.0:
            parts.append(f"R:R 1:{rr:.1f} (<1:2)")
        elif rr is None:
            parts.append("R:R нет")
        if ev is not None and ev <= 0:
            parts.append(f"мат.ожидание дохода {ev:+.2f}% (≤0)")
        elif ev is None:
            parts.append("мат.ожидание дохода нет")
        out["qwen_checklist_verdict"] = "Негативно: " + ", ".join(parts) + "."
    elif not ev_ok:
        ev_reason = f"мат.ожидание дохода {ev:+.2f}% (≤0)" if ev is not None else "мат.ожидание дохода нет"
        out["qwen_checklist_verdict"] = f"Негативно: {ev_reason}."
    else:
        out["qwen_checklist_verdict"] = (
            f"Негативно: R:R 1:{rr:.1f} (<1:2)."
        )
    return out


def build_5m_close_context(d5: Dict[str, Any]) -> Dict[str, Any]:
    """
    Контекст для закрытия позиции 5m (cron, бот).
    Возвращает dict для context_json в close_position(); все поля из get_decision_5m().
    """
    if not d5:
        return {}
    # Для закрытия позиции нам важно не только "close", а то, что цена могла коснуться
    # уровня тейка/стопа внутри последних 5m баров. Не подмешиваем session_high:
    # он может быть дальним/аномальным пиком сессии и давать ложный TAKE_PROFIT.
    bar_high_recent = d5.get("recent_bars_high_max") or d5.get("last_bar_high")
    bar_high = bar_high_recent
    bar_high_session_lifted = False

    bar_low = d5.get("recent_bars_low_min") or d5.get("last_bar_low")
    return {
        "momentum_2h_pct": d5.get("momentum_2h_pct"),
        "rsi_5m": d5.get("rsi_5m"),
        "bar_high": bar_high,
        "bar_high_recent_max": bar_high_recent,
        "bar_high_session_lifted": bar_high_session_lifted,
        "bar_low": bar_low,
        "exit_bar_close": d5.get("exit_bar_close"),
        "exit_bar_close_ts": d5.get("exit_bar_close_ts"),
        "exit_bar_start_et": d5.get("exit_bar_start_et"),
        "exit_bar_end_et": d5.get("exit_bar_end_et"),
        "decision_5m_bar_open_et": d5.get("decision_5m_bar_open_et"),
        "exit_5m_bar_open_et": d5.get("exit_5m_bar_open_et"),
        "exit_5m_bar_end_et": d5.get("exit_5m_bar_end_et"),
        "exit_5m_open": d5.get("exit_5m_open"),
        "exit_5m_high": d5.get("exit_5m_high"),
        "exit_5m_low": d5.get("exit_5m_low"),
        "exit_5m_close": d5.get("exit_5m_close"),
        "recent_bars_high_max": d5.get("recent_bars_high_max"),
        "recent_bars_high_ts": d5.get("recent_bars_high_ts"),
        "recent_bars_low_min": d5.get("recent_bars_low_min"),
        "recent_bars_low_ts": d5.get("recent_bars_low_ts"),
        "volatility_5m_pct": d5.get("volatility_5m_pct"),
        "period_str": d5.get("period_str"),
        "session_high": d5.get("session_high"),
        "session_high_ts": d5.get("session_high_ts"),
    }


def build_5m_trade_close_narrative(
    *,
    exit_type: str,
    exit_detail: str,
    entry_price: float,
    exit_price: float,
    take_pct: float,
    stop_pct: float,
    entry_ctx: Optional[Dict[str, Any]],
    exit_reasoning_excerpt: str = "",
) -> Dict[str, Optional[str]]:
    """
    Человекочитаемый разбор выхода и короткая сводка сделки (вход+выход) для context_json SELL и алертов.
    entry_ctx — обычно context_json с BUY (entry_condition, entry_intuition, decision, reasoning).
    """
    try:
        ep = float(entry_price)
        xp = float(exit_price)
        pnl_simple = ((xp / ep) - 1.0) * 100.0 if ep > 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        pnl_simple = 0.0
        ep, xp = entry_price, exit_price

    exit_condition: str
    exit_intuition: str

    if exit_type in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
        if exit_type == "TAKE_PROFIT_SUSPEND":
            exit_condition = (
                f"Условие выхода (тейк висяка): цель ~{take_pct:.2f}% после сужения по hanger / live-диагностике; допуск 0.05 п.п. "
                f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
            )
            exit_intuition = (
                "Интуиция: выход по облегчённому потолку тейка для подвисшей позиции (отдельно от обычного TAKE_PROFIT в отчётах)."
            )
        else:
            exit_condition = (
                f"Условие выхода (тейк): цель ~{take_pct:.2f}% от входа (импульс 2ч / потолок конфига; в кроне допуск 0.05 п.п.). "
                f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
            )
            exit_intuition = (
                "Интуиция: зафиксировать запланированный апсайд. Учёт High последних баров уменьшает шанс «пропустить» "
                "кратковременный всплеск между запусками крона по 5m Close."
            )
    elif exit_type == "STOP_LOSS":
        exit_condition = (
            f"Условие выхода (стоп): просадка от входа достигла ~{stop_pct:.2f}% (по логике Low бара). "
            f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
        )
        exit_intuition = (
            "Интуиция: жёсткое ограничение убытка по правилам GAME_5M; размер стопа согласован с тейком "
            "(STOP_TO_TAKE_RATIO и минимум из конфига)."
        )
    elif exit_type == "TIME_EXIT_EARLY":
        exit_condition = (
            "Условие выхода (ранний cut): долгое удержание, просадка и слабый импульс 2ч при HOLD/SELL — "
            f"ветка GAME_5M_EARLY_DERISK. Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
        )
        exit_intuition = (
            "Интуиция: не ждать тайм-аута по дням/минутам, если позиция «застряла» в минусе без поддержки краткосрочного тренда."
        )
    elif exit_type == "TIME_EXIT":
        if exit_detail == "session_end":
            exit_condition = (
                "Условие выхода (хвост сессии): до закрытия NYSE осталось ≤ GAME_5M_SESSION_END_EXIT_MINUTES "
                "и PnL ≥ GAME_5M_SESSION_END_MIN_PROFIT_PCT; при STRONG_BUY по текущему сигналу такой выход не делается. "
                f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
            )
            exit_intuition = (
                "Интуиция: не тащить интрадей через закрытие без сильного подтверждения — снизить overnight-гэп-риск."
            )
        elif exit_detail == "max_hold_minutes":
            exit_condition = (
                f"Условие выхода (лимит минут): превышен GAME_5M_MAX_POSITION_MINUTES. "
                f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
            )
            exit_intuition = "Интуиция: оборот капитала в быстрой 5m-игре; не держать позицию бесконечно без нового сигнала."
        elif exit_detail == "max_hold_days":
            exit_condition = (
                f"Условие выхода (лимит дней): превышен GAME_5M_MAX_POSITION_DAYS. "
                f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
            )
            exit_intuition = (
                "Интуиция: ограничить перенос позиции через несколько сессий — типичный контроль overnight-риска."
            )
        else:
            exit_condition = (
                f"Условие выхода (TIME_EXIT): лимит по времени удержания. "
                f"Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
            )
            exit_intuition = "Интуиция: принудительное закрытие по таймеру стратегии 5m."
    else:
        exit_condition = f"Условие выхода: тип {exit_type or '—'}. Факт: вход {ep:.4f}, выход {xp:.4f}, PnL ~{pnl_simple:+.2f}%."
        exit_intuition = "Интуиция: см. signal_type и логи game_5m.should_close_position."

    entry_recap_parts: List[str] = []
    if entry_ctx:
        ed = entry_ctx.get("decision") or entry_ctx.get("signal_type")
        if ed:
            entry_recap_parts.append(f"вход по решению {ed}")
        ec = (entry_ctx.get("entry_condition") or "").strip()
        if ec:
            entry_recap_parts.append(f"условие входа: {ec[:280]}{'…' if len(ec) > 280 else ''}")
        ei = (entry_ctx.get("entry_intuition") or "").strip()
        if ei:
            entry_recap_parts.append(f"интуиция входа: {ei[:220]}{'…' if len(ei) > 220 else ''}")
        if not ec and not ei:
            rs = (entry_ctx.get("reasoning") or "").strip()
            if rs:
                entry_recap_parts.append(f"контекст входа (reasoning): {rs[:240]}{'…' if len(rs) > 240 else ''}")
    entry_recap = " ".join(entry_recap_parts) if entry_recap_parts else "Вход: запись BUY без расшифрованного context_json (старые сделки)."

    exit_ctx_line = ""
    if exit_reasoning_excerpt.strip():
        exit_ctx_line = f" Контекст на момент выхода (5m): {exit_reasoning_excerpt.strip()[:200]}"

    trade_for_human = (
        f"Сводка сделки: {entry_recap} → выход {exit_type}"
        f"{(' / ' + exit_detail) if exit_detail else ''}: {exit_condition} {exit_intuition}{exit_ctx_line}"
    )
    if len(trade_for_human) > 3500:
        trade_for_human = trade_for_human[:3490] + "…"

    return {
        "exit_signal": exit_type,
        "exit_detail": exit_detail or None,
        "exit_condition": exit_condition,
        "exit_intuition": exit_intuition,
        "trade_for_human": trade_for_human,
        "entry_recap_for_human": entry_recap,
    }


def merge_close_context_with_trade_narrative(
    base: Dict[str, Any],
    *,
    d5: Dict[str, Any],
    exit_type: str,
    exit_detail: str,
    open_position: Dict[str, Any],
    entry_ctx: Optional[Dict[str, Any]],
    exit_price: float,
    take_pct: float,
    stop_pct: float,
) -> Dict[str, Any]:
    """Дополняет build_5m_close_context полями для человека; не мутирует исходный base."""
    out = dict(base) if base else {}
    try:
        entry_p = float(open_position.get("entry_price") or 0)
    except (TypeError, ValueError):
        entry_p = 0.0
    reasoning_excerpt = (d5.get("reasoning") or "") if isinstance(d5.get("reasoning"), str) else ""
    narrative = build_5m_trade_close_narrative(
        exit_type=exit_type,
        exit_detail=exit_detail or "",
        entry_price=entry_p,
        exit_price=float(exit_price),
        take_pct=float(take_pct),
        stop_pct=float(stop_pct),
        entry_ctx=entry_ctx,
        exit_reasoning_excerpt=reasoning_excerpt[:400],
    )
    out.update(narrative)
    return out


def _game5m_vix_context_snapshot() -> Dict[str, Any]:
    """Latest ^VIX close from DB + ~5-row change % for 5m reasoning (optional)."""
    from config_loader import get_config_value as gcv

    raw = (gcv("GAME_5M_VIX_CONTEXT_ENABLED", "true") or "true").strip().lower()
    if raw not in ("1", "true", "yes"):
        return {"enabled": False, "vix": None, "vix_change_multiday_pct": None}
    out: Dict[str, Any] = {"enabled": True, "vix": None, "vix_change_multiday_pct": None}
    try:
        from sqlalchemy import create_engine, text

        from config_loader import get_database_url

        eng = create_engine(get_database_url())
        with eng.connect() as conn:
            rows = conn.execute(
                text("SELECT close FROM quotes WHERE ticker = '^VIX' ORDER BY date DESC LIMIT 8")
            ).fetchall()
        eng.dispose()
    except Exception as e:
        logger.debug("VIX snapshot: %s", e)
        return out
    if not rows:
        return out
    try:
        spot = float(rows[0][0])
        out["vix"] = spot
        if len(rows) >= 6:
            old = float(rows[5][0])
            if old > 0:
                out["vix_change_multiday_pct"] = (spot - old) / old * 100.0
    except (TypeError, ValueError, IndexError):
        pass
    return out


def apply_kb_news_to_game5m_decision(
    *,
    kb_news: List[Dict[str, Any]],
    decision: str,
    reasons: List[str],
    technical_entry_branch: Optional[str],
    entry_strong_buy_downgraded: bool,
    rsi_5m: Optional[float],
    momentum_2h_pct: Any,
    decision_rule_params: Dict[str, Any],
) -> Tuple[str, List[str], Optional[str], bool, str, Dict[str, Any]]:
    """
    Учёт новостей KB (sentiment) и блока VIX — тот же смысл, что сразу после decide_game5m_technical
    в get_decision_5m. Используется и в офлайн-симуляции 30m для сопоставимости с кроном (без LLM).
    """
    from config_loader import get_config_value as _gcv

    news_with_sentiment = [(n, float(n["sentiment_score"])) for n in kb_news[:10] if n.get("sentiment_score") is not None]
    recent_negative = [n for n, s in news_with_sentiment if s < 0.4]
    very_negative = [n for n, s in news_with_sentiment if s < 0.35]
    recent_positive = [n for n, s in news_with_sentiment if s > 0.65]
    kb_news_impact = "нейтрально"

    vix_snap = _game5m_vix_context_snapshot()
    vv = vix_snap.get("vix")
    try:
        vix_c18 = float((_gcv("GAME_5M_VIX_STRONG_COMFORT_MAX", "18") or "18").strip())
    except (ValueError, TypeError):
        vix_c18 = 18.0
    try:
        vix_c20 = float((_gcv("GAME_5M_VIX_COMFORT_MAX", "20") or "20").strip())
    except (ValueError, TypeError):
        vix_c20 = 20.0
    relax_news_on_vix = (_gcv("GAME_5M_VIX_RELAX_VERY_NEGATIVE_NEWS", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    relax_very_neg = bool(
        very_negative and relax_news_on_vix and vv is not None and float(vv) < vix_c18
    )
    decision_rule_params["vix_comfort_max"] = vix_c20
    decision_rule_params["vix_strong_comfort_max"] = vix_c18
    decision_rule_params["vix_relax_very_negative_news"] = relax_news_on_vix

    branch = technical_entry_branch
    down = entry_strong_buy_downgraded
    dec = decision

    if very_negative:
        if relax_very_neg:
            reasons.append(
                "сильный негатив в новостях; VIX ниже порога «сильного спокойствия» — "
                "вход не откладываем только из-за гео-шума (рынок слабее дисконтирует панику)"
            )
            kb_news_impact = "негатив (VIX — вход не отложен)"
        elif dec in ("BUY", "STRONG_BUY"):
            dec = "HOLD"
            reasons.append("сильный негатив в новостях — вход отложен")
            kb_news_impact = "негатив (вход отложен)"
    elif recent_negative:
        if dec == "STRONG_BUY":
            dec = "BUY"
            down = True
            reasons.append("негативные новости в базе — вход без STRONG_BUY")
            kb_news_impact = "негатив (вход ослаблен)"
        elif dec == "BUY":
            reasons.append("негативные новости в базе — осторожность")
            kb_news_impact = "негатив (осторожность)"
    elif recent_positive and dec == "HOLD" and rsi_5m is not None and 38 <= rsi_5m <= 52 and momentum_2h_pct >= -0.5:
        dec = "BUY"
        branch = "buy_news_support"
        reasons.append("позитив в новостях поддерживает вход при нейтральном RSI")
        kb_news_impact = "позитив (поддержка входа)"
    elif recent_positive and dec in ("BUY", "STRONG_BUY"):
        kb_news_impact = "позитив"

    if vix_snap.get("enabled") and vv is not None:
        vp = [f"VIX={float(vv):.2f}"]
        chg = vix_snap.get("vix_change_multiday_pct")
        if chg is not None:
            vp.append(f"Δ≈5сессий {float(chg):+.1f}%")
        if float(vv) < vix_c18:
            vp.append("<18 — сжатая премия за риск")
        elif float(vv) < vix_c20:
            vp.append("<20 — ослабление паники по индексу")
        reasons.append(" · ".join(vp))

    return dec, reasons, branch, down, kb_news_impact, vix_snap


def get_decision_5m(
    ticker: str,
    days: int = None,
    use_llm_news: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Решение по 5m для короткой игры: 5m-свечи + новости из KB за тот же период.
    Влияние новостей на решение явное: сильный негатив (sentiment < 0.35) откладывает
    вход (BUY→HOLD), негатив (0.35–0.4) ослабляет (STRONG_BUY→BUY); позитив (> 0.65)
    при пограничном RSI может поддержать вход (HOLD→BUY). Результат возвращает
    kb_news_impact для отображения в алерте.

    use_llm_news: при True и USE_LLM_NEWS — запрос к LLM о свежих новостях (дополняет KB).
    LLM не ищет в интернете; breaking news должны быть в KB (cron: Investing.com News и т.д.).

    Returns:
        dict: decision, reasoning, price, rsi_5m, volatility_5m_pct, momentum_2h_pct,
        high_5d, low_5d, period_str, bars_count, stop_loss_pct, take_profit_pct;
        при BUY/STRONG_BUY: technical_entry_branch, entry_condition (формальные пороги + факт),
        entry_intuition (смысл ветки правила), entry_strong_buy_downgraded при ослаблении STRONG_BUY→BUY из‑за новостей;
        kb_news_days, kb_news — новости из KB за тот же период; market_session — открытие/закрытие биржи и праздники (отдельно для учёта особых процессов новостей);
        при use_llm_news — llm_news_content, llm_sentiment, llm_insight.
        None только если нет ни одного бара (Yahoo не вернул данных).
    """
    if days is None:
        days = MAX_DAYS_5M
    df = fetch_5m_ohlc(ticker, days=days)
    if df is None or df.empty:
        logger.warning("Нет 5m данных для %s за последние %d дн.", ticker, days)
        return None

    # Все технические параметры из 5m — один раз; переиспользуются для правил, context_json, LLM/ML
    features = compute_5m_features(df, ticker)
    if features is None:
        return None
    # Ряд закрытий для RSI на срезах (подтверждение SELL по нескольким барам)
    closes = df["Close"].astype(float)
    price = features["price"]
    high_5d = features["high_5d"]
    low_5d = features["low_5d"]
    rsi_5m = features["rsi_5m"]
    volatility_5m_pct = features["volatility_5m_pct"]
    momentum_2h_pct = features["momentum_2h_pct"]
    session_high = features["session_high"]
    pullback_from_high_pct = features["pullback_from_high_pct"]
    last_bar_high = features["last_bar_high"]
    last_bar_low = features["last_bar_low"]
    recent_bars_high_max = features["recent_bars_high_max"]
    recent_bars_low_min = features["recent_bars_low_min"]
    curvature_5m_pct = features.get("curvature_5m_pct")
    possible_bounce_to_high_pct = features.get("possible_bounce_to_high_pct")
    estimated_bounce_pct = features.get("estimated_bounce_pct")
    period_str = features["period_str"]

    # Правила решения (агрессивные под интрадей)
    from config_loader import get_config_value as _gcv

    market_session: Dict[str, Any] = {}
    try:
        from services.market_session import get_market_session_context

        market_session = get_market_session_context()
    except Exception as e:
        logger.debug("Контекст сессии биржи для 5m: %s", e)
    session_phase = (market_session.get("session_phase") or "").strip()

    th = get_decision_5m_rule_thresholds()
    sell_confirm_bars = int(th["sell_confirm_bars"])
    min_sess_bars = int(th["momentum_min_session_bars"])
    vol_wait_min = float(th["volatility_wait_min"])
    rsi_strong_buy_max = float(th["rsi_strong_buy_max"])
    momentum_for_strong_buy_min = float(th["momentum_for_strong_buy_min"])
    rsi_buy_max = float(th["rsi_buy_max"])
    price_to_low5d_multiplier_max = float(th["price_to_low5d_multiplier_max"])
    rsi_sell_min = float(th["rsi_sell_min"])
    rsi_hold_overbought_min = float(th["rsi_hold_overbought_min"])
    rth_momentum_buy_min = float(th["momentum_buy_min"])
    rsi_for_momentum_buy_max = float(th["rsi_for_momentum_buy_max"])
    volatility_warn_buy_min = float(th["volatility_warn_buy_min"])
    pm_mom_buy_min = float(th["premarket_momentum_buy_min"])
    pm_mom_block_below = float(th["premarket_momentum_block_below"])
    allow_cross_day_mom_buy = (_gcv("GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    early_use_premarket_mom = (_gcv("GAME_5M_EARLY_USE_PREMARKET_MOMENTUM", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    mom_rth_bars = int(features.get("momentum_rth_today_bars") or 0)
    rth_intraday_active = _rth_intraday_entry_active(session_phase, mom_rth_bars, min_sess_bars)
    premarket_intraday_momentum_pct: Optional[float] = None
    if early_use_premarket_mom and mom_rth_bars < min_sess_bars:
        try:
            from services.premarket import get_premarket_intraday_momentum_pct

            premarket_intraday_momentum_pct = get_premarket_intraday_momentum_pct(ticker)
        except Exception as e:
            logger.debug("premarket intraday momentum для %s: %s", ticker, e)
    rsi_prev_values: List[float] = []
    for back in range(1, sell_confirm_bars + 1):
        if len(closes) > back + RSI_PERIOD_5M:
            rv = compute_rsi_5m(closes.iloc[: -back], period=RSI_PERIOD_5M)
            if rv is not None:
                rsi_prev_values.append(float(rv))
    decision_rule_params: Dict[str, Any] = {
        "rule_version": GAME_5M_RULE_VERSION,
        "source_fn": "services.recommend_5m.get_decision_5m",
        "rsi_strong_buy_max": rsi_strong_buy_max,
        "momentum_for_strong_buy_min": momentum_for_strong_buy_min,
        "rsi_buy_max": rsi_buy_max,
        "price_to_low5d_multiplier_max": price_to_low5d_multiplier_max,
        "rsi_sell_min": rsi_sell_min,
        "rsi_hold_overbought_min": rsi_hold_overbought_min,
        "momentum_buy_min": rth_momentum_buy_min,
        "rsi_for_momentum_buy_max": rsi_for_momentum_buy_max,
        "volatility_warn_buy_min": volatility_warn_buy_min,
        "volatility_wait_min": vol_wait_min,
        "sell_confirm_bars": sell_confirm_bars,
        "news_negative_min": 0.4,
        "news_very_negative_min": 0.35,
        "news_positive_min": 0.65,
        "momentum_min_session_bars": min_sess_bars,
        "momentum_allow_cross_day_buy": allow_cross_day_mom_buy,
        "early_use_premarket_momentum": early_use_premarket_mom,
        "premarket_momentum_buy_min": pm_mom_buy_min,
        "premarket_momentum_block_below": pm_mom_block_below,
    }

    decision, reasons, technical_entry_branch, entry_strong_buy_downgraded = decide_game5m_technical(
        ticker=ticker,
        features=features,
        closes=closes,
        th=th,
        rsi_prev_values=rsi_prev_values,
        decision_rule_params=decision_rule_params,
        min_session_bars=min_sess_bars,
        premarket_intraday_momentum_pct=premarket_intraday_momentum_pct,
        early_use_premarket_mom=early_use_premarket_mom,
        session_phase=session_phase,
    )

    # Влияние новостей из KB на короткую игру 5m: явный учёт в решении BUY/HOLD/SELL
    kb_news = fetch_kb_news_for_period(ticker, days)
    decision, reasons, technical_entry_branch, entry_strong_buy_downgraded, kb_news_impact, vix_snap = apply_kb_news_to_game5m_decision(
        kb_news=kb_news,
        decision=decision,
        reasons=reasons,
        technical_entry_branch=technical_entry_branch,
        entry_strong_buy_downgraded=entry_strong_buy_downgraded,
        rsi_5m=rsi_5m,
        momentum_2h_pct=momentum_2h_pct,
        decision_rule_params=decision_rule_params,
    )

    # Ниже (entry_advice, премаркет-ветки) используются флаги негативных новостей и relax_very_neg.
    # Они считаются внутри apply_kb_news_to_game5m_decision, но здесь нужны в области видимости.
    news_with_sentiment = [(n, float(n["sentiment_score"])) for n in kb_news[:10] if n.get("sentiment_score") is not None]
    recent_negative = [n for n, s in news_with_sentiment if s < 0.4]
    very_negative = [n for n, s in news_with_sentiment if s < 0.35]
    vv = (vix_snap or {}).get("vix")
    try:
        vix_c18_local = float((_gcv("GAME_5M_VIX_STRONG_COMFORT_MAX", "18") or "18").strip())
    except (ValueError, TypeError):
        vix_c18_local = 18.0
    relax_news_on_vix_local = (_gcv("GAME_5M_VIX_RELAX_VERY_NEGATIVE_NEWS", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    relax_very_neg = bool(very_negative and relax_news_on_vix_local and vv is not None and float(vv) < vix_c18_local)

    if not reasons:
        rsi_str = f"{rsi_5m:.1f}" if rsi_5m is not None else "— (мало баров)"
        reasons.append(
            f"5m: цена {price:.2f}, RSI={rsi_str}, импульс 2ч={momentum_2h_pct:+.2f}%, волатильность={volatility_5m_pct:.2f}%, баров={len(df)}"
        )
    elif session_phase == "PRE_MARKET" and not rth_intraday_active:
        reasons.append(
            "Премаркет: RTH-импульс ещё не начался — intraday BUY по momentum отключён; ориентир: multiday ridge, PM gap, macro, 5d low"
        )

    reasoning = " ".join(reasons)

    # Параметры стратегии 5m из конфига (config.env: GAME_5M_STOP_LOSS_PCT, GAME_5M_TAKE_PROFIT_PCT, GAME_5M_STOP_LOSS_ENABLED)
    from config_loader import get_config_value
    try:
        stop_loss_pct = float(get_config_value("GAME_5M_STOP_LOSS_PCT", "2.5"))
    except (ValueError, TypeError):
        stop_loss_pct = 2.5
    try:
        take_profit_pct = float(get_config_value("GAME_5M_TAKE_PROFIT_PCT", "5.0"))
    except (ValueError, TypeError):
        take_profit_pct = 5.0
    _sl_raw = (get_config_value("GAME_5M_STOP_LOSS_ENABLED", "true") or "true").strip().lower()
    stop_loss_enabled = _sl_raw in ("1", "true", "yes")

    # KB уже загружены выше для учёта негатива в решении; передаём в контекст
    # (market_session уже загружен до decide_game5m_technical)
    premarket_context = None
    if session_phase == "PRE_MARKET":
        try:
            from services.premarket import get_premarket_context
            premarket_context = get_premarket_context(ticker)
            if premarket_context.get("error"):
                reasoning = reasoning + f" [Премаркет: нет данных]"
            else:
                pm_last = premarket_context.get("premarket_last")
                gap = premarket_context.get("premarket_gap_pct")
                mins = premarket_context.get("minutes_until_open") or market_session.get("minutes_until_open")
                parts = []
                if pm_last is not None:
                    parts.append(f"премаркет {pm_last:.2f}")
                if gap is not None:
                    parts.append(f"гэп к закрытию {gap:+.2f}%")
                if mins is not None:
                    parts.append(f"до открытия {mins} мин")
                if parts:
                    reasoning = reasoning + f" [{' | '.join(parts)}. Ликвидность ниже.]"
        except Exception as e:
            logger.debug("Премаркет для %s: %s", ticker, e)
    if session_phase in ("PRE_MARKET", "AFTER_HOURS", "WEEKEND", "HOLIDAY") and decision in ("BUY", "STRONG_BUY"):
        reasoning = reasoning + f" [Вход отложен: сессия {session_phase} — ждём открытия биржи 9:30 ET]"
        decision = "HOLD"

    # Осторожность на старте RTH: первый 5m бар часто «широкий» (гэп/аукцион),
    # а `price` по умолчанию = Close последнего бара может не совпадать с тем, что интуитивно видно как «цена открытия».
    # Здесь мы НЕ пересчитываем RSI/импульс (они уже посчитаны), но можем:
    # - сделать recorded entry price более консервативным (min(Open, Close) последнего бара)
    # - и/или отложить агрессивный STRONG_BUY до стабилизации после широкого open-бара.
    near_open_guard = (_gcv("GAME_5M_NEAR_OPEN_BUY_GUARD", "true") or "true").strip().lower() in ("1", "true", "yes")
    try:
        near_open_thr = float((_gcv("GAME_5M_NEAR_OPEN_WIDE_BAR_PCT", "1.0") or "1.0").strip())
    except (ValueError, TypeError):
        near_open_thr = 1.0
    try:
        near_open_first_min = int(
            float((_gcv("GAME_5M_NEAR_OPEN_FIRST_WINDOW_MIN", "15") or "15").strip())
        )
    except (ValueError, TypeError):
        near_open_first_min = 15
    near_open_first_min = max(1, min(near_open_first_min, 120))
    # strong: что делать с STRONG_BUY на «широком» баре в окне после открытия: hold | buy | none
    near_open_strong_mode = (
        (_gcv("GAME_5M_NEAR_OPEN_STRONG_BUY_ON_WIDE_BAR", "hold") or "hold").strip().lower()
    )
    if near_open_strong_mode in ("off", "skip", "no", "false", "0"):
        near_open_strong_mode = "none"
    # buy: что делать с обычным BUY (обычно оставляем, иначе слишком много пропусков)
    near_open_buy_mode = ((_gcv("GAME_5M_NEAR_OPEN_BUY_ON_WIDE_BAR", "none") or "none").strip().lower())
    if near_open_buy_mode in ("off", "skip", "no", "false", "0"):
        near_open_buy_mode = "none"
    decision_rule_params.update(
        {
            "near_open_buy_guard": near_open_guard,
            "near_open_wide_bar_pct": near_open_thr,
            "near_open_first_window_min": near_open_first_min,
            "near_open_strong_buy_on_wide_bar": near_open_strong_mode,
            "near_open_buy_on_wide_bar": near_open_buy_mode,
        }
    )
    open_guard_triggered = False
    open_guard_bar_range_pct: Optional[float] = None
    open_guard_mins_since_open: Optional[float] = None
    open_guard_prev_decision: Optional[str] = None
    open_guard_next_decision: Optional[str] = None
    entry_price_basis = "last_close"
    if (
        near_open_guard
        and session_phase == "NEAR_OPEN"
        and decision in ("BUY", "STRONG_BUY")
        and df is not None
        and not df.empty
        and all(c in df.columns for c in ("Open", "Close"))
    ):
        try:
            t_last = pd.to_datetime(df["datetime"].iloc[-1])
            if t_last.tzinfo is None:
                t_last = t_last.tz_localize("America/New_York", ambiguous="infer")
            else:
                t_last = t_last.tz_convert("America/New_York")
            open_et = t_last.normalize() + pd.Timedelta(hours=9, minutes=30)
            mins_since_open = float((t_last - open_et).total_seconds()) / 60.0
            in_open_window = mins_since_open >= 0 and mins_since_open <= float(near_open_first_min)
            open_guard_mins_since_open = mins_since_open
            o_last = float(pd.to_numeric(df["Open"].iloc[-1], errors="coerce"))
            c_last = float(pd.to_numeric(df["Close"].iloc[-1], errors="coerce"))
            if in_open_window and o_last > 0 and c_last > 0:
                bar_range_pct = abs(o_last - c_last) / max(o_last, c_last) * 100.0
                # Консервативная цена записи входа: не «хуже» min(Open, Close) на широком баре
                if bar_range_pct >= near_open_thr:
                    open_guard_triggered = True
                    open_guard_bar_range_pct = float(bar_range_pct)
                    decision_before_wide_bar = decision
                    new_px = float(min(o_last, c_last))
                    if new_px > 0 and new_px != float(price):
                        old_px = float(price)
                        price = new_px
                        features["price"] = new_px
                        entry_price_basis = "min_open_close_last_bar"
                        reasoning = (
                            reasoning
                            + f" [Open-guard: NEAR_OPEN first {near_open_first_min}m, wide 5m bar ~{bar_range_pct:.2f}% "
                            f"≥ {near_open_thr:.2f}% → entry_price_basis={entry_price_basis} ({old_px:.4f}→{new_px:.4f})]"
                        )
                    if decision_before_wide_bar == "STRONG_BUY" and near_open_strong_mode in ("hold", "buy", "none"):
                        if near_open_strong_mode == "hold":
                            decision = "HOLD"
                            reasoning = reasoning + " [Open-guard: STRONG_BUY отложен из‑за широкого бара у открытия]"
                        elif near_open_strong_mode == "buy":
                            decision = "BUY"
                            reasoning = reasoning + " [Open-guard: STRONG_BUY→BUY из‑за широкого бара у открытия]"
                    elif decision_before_wide_bar == "BUY" and near_open_buy_mode == "hold":
                        decision = "HOLD"
                        reasoning = reasoning + " [Open-guard: BUY отложен из‑за широкого бара у открытия]"
                    open_guard_prev_decision = str(decision_before_wide_bar)
                    open_guard_next_decision = str(decision)
        except Exception as e:
            logger.debug("near_open buy guard для %s: %s", ticker, e)

    entry_stale_chase_guard_triggered = False
    entry_stale_chase_guard_prev_decision: Optional[str] = None
    entry_stale_chase_guard_reason: Optional[str] = None
    decision, reasons, entry_stale_chase_guard_triggered, entry_stale_chase_guard_prev_decision, entry_stale_chase_guard_reason = (
        apply_entry_stale_chase_guard(decision, reasons, features, rth_intraday_active=rth_intraday_active)
    )
    if entry_stale_chase_guard_triggered:
        reasoning = " ".join(reasons)

    # Рекомендация по входу в премаркете (2.2, 2.3): войти сейчас / ждать открытия / лимит ниже
    premarket_entry_recommendation = None
    premarket_suggested_limit_price = None
    if session_phase == "PRE_MARKET" and premarket_context and not premarket_context.get("error"):
        gap = premarket_context.get("premarket_gap_pct")
        pm_last = premarket_context.get("premarket_last")
        prev_cl = premarket_context.get("prev_close")
        if gap is not None and pm_last is not None:
            if gap < -1.5 and (
                (momentum_2h_pct is not None and momentum_2h_pct < 0)
                or (very_negative and not relax_very_neg)
            ):
                limit_pct = 0.5
                premarket_suggested_limit_price = round(pm_last * (1 - limit_pct / 100.0), 2) if pm_last else None
                limit_str = f"${premarket_suggested_limit_price:.2f}" if premarket_suggested_limit_price is not None else "—"
                premarket_entry_recommendation = (
                    f"Цена идёт вниз (гэп {gap:+.2f}%, импульс 2ч отрицательный или негатив). "
                    f"Рекомендация: войти после открытия 9:30 ET или лимит ниже {limit_str}."
                )
            elif gap >= -0.5:
                premarket_entry_recommendation = (
                    "Можно рассмотреть вход по текущей цене премаркета (ликвидность ниже обычной)."
                )
            else:
                limit_pct = 0.5
                premarket_suggested_limit_price = round(pm_last * (1 - limit_pct / 100.0), 2) if pm_last else None
                limit_str = f"${premarket_suggested_limit_price:.2f}" if premarket_suggested_limit_price is not None else "—"
                premarket_entry_recommendation = (
                    f"Рекомендация: войти после открытия 9:30 ET или лимит ниже {limit_str}."
                )

    # ---- Бар 5m решения о входе = последняя свеча ряда (постоянен между минутными запусками крона, пока не появится новый бар) ----
    entry_5m_bar_open_et = None
    entry_5m_bar_end_et = None
    entry_5m_open = entry_5m_high = entry_5m_low = entry_5m_close = None
    try:
        if df is not None and not df.empty and "datetime" in df.columns:
            row = df.iloc[-1]
            if all(c in df.columns for c in ("Open", "High", "Low", "Close")):
                entry_5m_open = float(row["Open"])
                entry_5m_high = float(row["High"])
                entry_5m_low = float(row["Low"])
                entry_5m_close = float(row["Close"])
            dt_last = pd.to_datetime(row["datetime"], errors="coerce")
            if pd.notna(dt_last):
                if getattr(dt_last, "tzinfo", None) is None:
                    dt_et = dt_last.tz_localize("America/New_York", ambiguous="infer")
                else:
                    dt_et = dt_last.tz_convert("America/New_York")
                entry_5m_bar_open_et = dt_et.isoformat()
                entry_5m_bar_end_et = (dt_et + pd.Timedelta(minutes=5)).isoformat()
    except Exception:
        pass

    # Close бара, который только что завершился к текущему моменту (для записи цены выхода — без расхождений с корректором)
    exit_bar_close = None
    exit_bar_close_ts = None
    exit_bar_start_et = None
    exit_bar_end_et = None
    exit_5m_bar_open_et = None
    exit_5m_bar_end_et = None
    exit_5m_open = exit_5m_high = exit_5m_low = exit_5m_close = None
    # Открытие 5m-бара из df по тому же окну, что exit_bar_close — не меняется между минутными запусками крона, пока бар тот же.
    decision_5m_bar_open_et = None
    try:
        now_et = pd.Timestamp.now(tz="America/New_York")
        bar_end = now_et.floor("5min")
        bar_start = bar_end - pd.Timedelta(minutes=5)
        exit_bar_start_et = bar_start.isoformat()
        exit_bar_end_et = bar_end.isoformat()
        decision_5m_bar_open_et = exit_bar_start_et
        dts = pd.to_datetime(df["datetime"])
        if dts.dt.tz is None:
            dts = dts.dt.tz_localize("America/New_York", ambiguous="infer")
        else:
            dts = dts.dt.tz_convert("America/New_York")
        mask = (dts >= bar_start) & (dts < bar_end)
        if mask.any():
            exit_bar_close = float(df.loc[mask, "Close"].iloc[-1])
            try:
                exr = df.loc[mask].iloc[-1]
                if all(c in df.columns for c in ("Open", "High", "Low", "Close")):
                    exit_5m_open = float(exr["Open"])
                    exit_5m_high = float(exr["High"])
                    exit_5m_low = float(exr["Low"])
                    exit_5m_close = float(exr["Close"])
            except Exception:
                pass
            try:
                t_exit = pd.to_datetime(df.loc[mask, "datetime"].iloc[-1])
                if t_exit.tzinfo is None:
                    t_exit = t_exit.tz_localize("America/New_York", ambiguous="infer")
                else:
                    t_exit = t_exit.tz_convert("America/New_York")
                exit_bar_close_ts = t_exit.isoformat()
            except Exception:
                exit_bar_close_ts = None
            try:
                t_open_series = pd.to_datetime(df.loc[mask, "datetime"])
                t_open_min = t_open_series.min()
                if getattr(t_open_min, "tzinfo", None) is None:
                    t_open_min = t_open_min.tz_localize("America/New_York", ambiguous="infer")
                else:
                    t_open_min = t_open_min.tz_convert("America/New_York")
                decision_5m_bar_open_et = t_open_min.isoformat()
            except Exception:
                pass
        if exit_bar_close is None or exit_bar_close <= 0:
            exit_bar_close = price
    except Exception:
        exit_bar_close = price

    exit_5m_bar_open_et = decision_5m_bar_open_et
    exit_5m_bar_end_et = exit_bar_end_et

    # Базовый вывод: все признаки из compute_5m_features (один раз посчитаны) + решение и контекст
    out = {
        **features,
        "decision": decision,
        "reasoning": reasoning,
        "decision_rule_version": GAME_5M_RULE_VERSION,
        "decision_rule_params": decision_rule_params,
        "technical_entry_branch": technical_entry_branch,
        "entry_strong_buy_downgraded": entry_strong_buy_downgraded,
        "exit_bar_close": exit_bar_close,
        "exit_bar_close_ts": exit_bar_close_ts,
        "exit_bar_start_et": exit_bar_start_et,
        "exit_bar_end_et": exit_bar_end_et,
        "decision_5m_bar_open_et": decision_5m_bar_open_et,
        "entry_5m_bar_open_et": entry_5m_bar_open_et,
        "entry_5m_bar_end_et": entry_5m_bar_end_et,
        "entry_5m_open": entry_5m_open,
        "entry_5m_high": entry_5m_high,
        "entry_5m_low": entry_5m_low,
        "entry_5m_close": entry_5m_close,
        "exit_5m_bar_open_et": exit_5m_bar_open_et,
        "exit_5m_bar_end_et": exit_5m_bar_end_et,
        "exit_5m_open": exit_5m_open,
        "exit_5m_high": exit_5m_high,
        "exit_5m_low": exit_5m_low,
        "exit_5m_close": exit_5m_close,
        "stop_loss_enabled": stop_loss_enabled,
        "stop_loss_pct": stop_loss_pct if stop_loss_enabled else None,
        "take_profit_pct": take_profit_pct,
        "kb_news_days": days,
        "kb_news": kb_news,
        "kb_news_impact": kb_news_impact,
        "market_session": market_session,
        "session_phase": session_phase,
        "entry_price_basis": entry_price_basis,
        "open_guard_triggered": open_guard_triggered,
        "open_guard_bar_range_pct": open_guard_bar_range_pct,
        "open_guard_mins_since_open": open_guard_mins_since_open,
        "open_guard_prev_decision": open_guard_prev_decision,
        "open_guard_next_decision": open_guard_next_decision,
        "entry_stale_chase_guard_triggered": entry_stale_chase_guard_triggered,
        "entry_stale_chase_guard_prev_decision": entry_stale_chase_guard_prev_decision,
        "entry_stale_chase_guard_reason": entry_stale_chase_guard_reason,
    }
    if vix_snap.get("enabled"):
        out["vix_context"] = {
            "vix": vix_snap.get("vix"),
            "vix_change_multiday_pct": vix_snap.get("vix_change_multiday_pct"),
        }
    if premarket_intraday_momentum_pct is not None:
        out["premarket_intraday_momentum_pct"] = premarket_intraday_momentum_pct
    if premarket_context is not None:
        out["premarket_context"] = premarket_context
        out["premarket_last"] = premarket_context.get("premarket_last")
        out["premarket_gap_pct"] = premarket_context.get("premarket_gap_pct")
        out["prev_close"] = premarket_context.get("prev_close")
        if premarket_context.get("minutes_until_open") is not None:
            out["minutes_until_open"] = premarket_context.get("minutes_until_open")
        elif market_session.get("minutes_until_open") is not None:
            out["minutes_until_open"] = market_session.get("minutes_until_open")
        # В премаркете показываем текущую цену премаркета как price (5m баров ещё нет)
        if premarket_context.get("premarket_last") is not None:
            out["price"] = premarket_context["premarket_last"]
        if premarket_entry_recommendation is not None:
            out["premarket_entry_recommendation"] = premarket_entry_recommendation
        if premarket_suggested_limit_price is not None:
            out["premarket_suggested_limit_price"] = premarket_suggested_limit_price
    elif session_phase != "PRE_MARKET":
        rth_gap, rth_open, prev_cl = _compute_rth_open_gap_pct(df, ticker)
        if rth_gap is not None:
            out["rth_open_gap_pct"] = rth_gap
        if rth_open is not None:
            out["rth_open_price"] = rth_open
        if prev_cl is not None and out.get("prev_close") is None:
            out["prev_close"] = prev_cl
        if rth_gap is not None:
            try:
                from services.game5m_gap_forecast import record_open_gap_for_symbol

                record_open_gap_for_symbol(
                    ticker,
                    open_gap_pct=rth_gap,
                    rth_open_price=rth_open,
                    prev_close=prev_cl,
                    source_open="rth_5m_lazy",
                )
            except Exception as e_gfl:
                logger.debug("gap_forecast open lazy %s: %s", ticker, e_gfl)

    # Оценка апсайда/тейка для входа:
    # - база: эффективный тейк из действующей логики (импульс + тикерный cap);
    # - при наличии прогноза 5m (60/120) — пересчитываем цель из p50, но ограничиваем тикерным cap;
    # - минимально интересная цель для игры: 4%.
    try:
        from services.game_5m import _effective_take_profit_pct, _take_profit_cap_pct
        effective_take_pct = _effective_take_profit_pct(momentum_2h_pct, ticker=ticker)
        out["estimated_upside_pct_day"] = effective_take_pct
        out["take_profit_pct"] = effective_take_pct
        p = out.get("price") or price
        if p is not None and p > 0:
            out["suggested_take_profit_price"] = round(p * (1 + effective_take_pct / 100.0), 2)
    except Exception as e:
        logger.debug("estimated_upside для 5m: %s", e)

    # Оценка downside (риск просадки), НЕ стоп-лосс (4.1b)
    # Идея: размер потенциального хода вниз на горизонте дня, чтобы понимать risk/reward без привязки к стопу.
    try:
        p = out.get("price") or price
        recent_low = out.get("recent_bars_low_min")
        vol_5m = out.get("volatility_5m_pct")
        mom_2h = out.get("momentum_2h_pct")
        rsi_now = out.get("rsi_5m")
        phase = (out.get("market_session") or {}).get("session_phase") or out.get("session_phase")

        downside_to_recent_low = None
        if p is not None and p > 0 and isinstance(recent_low, (int, float)) and recent_low > 0 and recent_low < p:
            downside_to_recent_low = (p - float(recent_low)) / float(p) * 100.0

        base = 0.0
        if isinstance(vol_5m, (int, float)) and vol_5m > 0:
            # Волатильность 5m — std лог-доходностей * 100. Для дневного риска берём консервативный мультипликатор.
            base = max(base, float(vol_5m) * 2.5)
        if isinstance(downside_to_recent_low, (int, float)) and downside_to_recent_low > 0:
            base = max(base, float(downside_to_recent_low))

        # Усиление риска при отрицательном импульсе / премаркете (ликвидность ниже)
        if isinstance(mom_2h, (int, float)) and mom_2h < -1.0:
            base *= 1.2
        if phase in ("PRE_MARKET", "AFTER_HOURS"):
            base *= 1.1

        # Если RSI очень низкий (перепроданность) — downside риск обычно меньше, чем при перегреве
        if isinstance(rsi_now, (int, float)) and rsi_now <= 25:
            base *= 0.85
        elif isinstance(rsi_now, (int, float)) and rsi_now >= 75:
            base *= 1.15

        # Ограничения, чтобы не раздувать и не получать нули
        estimated_downside = round(min(max(base, 0.2), 25.0), 2) if base and base > 0 else None
        out["estimated_downside_pct_day"] = estimated_downside

        # Грубая вероятность направления (для скана): prob_up + prob_down = 1
        up_score = 1.0
        down_score = 1.0
        if isinstance(rsi_now, (int, float)):
            if rsi_now <= 30:
                up_score += 0.6
            elif rsi_now >= 70:
                down_score += 0.6
        if isinstance(mom_2h, (int, float)):
            if mom_2h >= 1.0:
                up_score += 0.4
            elif mom_2h <= -1.0:
                down_score += 0.4
        if isinstance(vol_5m, (int, float)) and vol_5m >= 0.8:
            # высокая волатильность делает направление менее определённым
            up_score = 1.0 + (up_score - 1.0) * 0.7
            down_score = 1.0 + (down_score - 1.0) * 0.7
        s = up_score + down_score
        out["prob_up"] = round(up_score / s, 2)
        out["prob_down"] = round(down_score / s, 2)
    except Exception as e:
        logger.debug("estimated_downside для 5m: %s", e)

    # Краткосрочный прогноз цены (квантили p10/p50/p90, P(>spot)) — docs/GAME_5M_PRICE_FORECAST.md
    try:
        from services.price_forecast_5m import compute_price_forecast_5m, format_price_forecast_one_line

        p_spot = float(out.get("price") or price)
        fc = compute_price_forecast_5m(closes, p_spot)
        if fc:
            out["price_forecast_5m"] = fc
            out["price_forecast_5m_summary"] = format_price_forecast_one_line(fc)
            # Новая цель: 60/120 mix по p50, с ограничением достижимости и тикерным cap.
            by_m = {}
            for h in (fc.get("horizons") or []):
                try:
                    m = int(h.get("minutes"))
                except (TypeError, ValueError):
                    continue
                by_m[m] = h
            h60 = by_m.get(60)
            h120 = by_m.get(120)
            try:
                p50_60 = float(h60.get("p50_pct_vs_spot")) if h60 else None
            except (TypeError, ValueError):
                p50_60 = None
            try:
                p50_120 = float(h120.get("p50_pct_vs_spot")) if h120 else None
            except (TypeError, ValueError):
                p50_120 = None
            try:
                p90_120 = float(h120.get("p90_pct_vs_spot")) if h120 else None
            except (TypeError, ValueError):
                p90_120 = None

            vals = []
            if p50_60 is not None:
                vals.append((0.6, max(0.0, p50_60)))
            if p50_120 is not None:
                vals.append((0.4, max(0.0, p50_120)))
            if vals:
                w_sum = sum(w for w, _ in vals)
                target_pct = sum(w * v for w, v in vals) / w_sum if w_sum > 0 else None
                if target_pct is not None and p90_120 is not None and p90_120 > 0:
                    # Не ставим цель на хвосте распределения.
                    target_pct = min(target_pct, 0.60 * p90_120)
                ticker_cap = _take_profit_cap_pct(ticker)
                target_pct = min(target_pct, float(ticker_cap))
                min_target = 4.0
                # До пола 4%: «реальный» апсайд модели (для карточки рядом с эффективным тейком).
                if target_pct is not None:
                    try:
                        out["estimated_upside_forecast_raw_pct"] = round(float(target_pct), 2)
                    except (TypeError, ValueError):
                        pass
                if ticker_cap < min_target:
                    if decision in ("BUY", "STRONG_BUY"):
                        decision_prev = decision
                        decision = "HOLD"
                        note = (
                            f"target guard: тикерный потолок тейка {ticker_cap:.2f}% < минимально интересного {min_target:.2f}%"
                        )
                        reasoning = (reasoning + " " + note).strip()
                        out["decision"] = decision
                        out["reasoning"] = reasoning
                        out["entry_target_guard_prev_decision"] = decision_prev
                        out["entry_target_guard_reason"] = note
                    out["target_mode"] = "forecast_60_120_blocked_by_low_cap"
                else:
                    target_pct = max(min_target, target_pct)
                    out["take_profit_pct"] = round(target_pct, 2)
                    out["estimated_upside_pct_day"] = round(target_pct, 2)
                    p = out.get("price") or price
                    if p is not None and p > 0:
                        out["suggested_take_profit_price"] = round(p * (1 + target_pct / 100.0), 2)
                    out["target_mode"] = "forecast_60_120"
    except Exception as e:
        logger.debug("price_forecast_5m: %s", e)

    # Наивный мультидневной прогноз лог-доходности: ridge на дневных close + хвост 5m в точке прогноза (опционально).
    try:
        mlr_on = (_gcv("GAME_5M_MULTIDAY_LR_REG_ENABLED", "false") or "false").strip().lower() in ("1", "true", "yes")
    except Exception:
        mlr_on = False
    if mlr_on:
        try:
            from services.log_return_multiday_forecast import (
                compute_log_return_multiday_forecast,
                format_multiday_forecast_one_line,
            )

            try:
                mlr_period = int((_gcv("GAME_5M_MULTIDAY_LR_REG_PERIOD_DAYS", "400") or "400").strip())
            except (ValueError, TypeError):
                mlr_period = 400
            try:
                mlr_lam = float((_gcv("GAME_5M_MULTIDAY_LR_REG_RIDGE_LAMBDA", "1.0") or "1.0").strip())
            except (ValueError, TypeError):
                mlr_lam = 1.0
            try:
                mlr_min = int((_gcv("GAME_5M_MULTIDAY_LR_REG_MIN_SAMPLES", "80") or "80").strip())
            except (ValueError, TypeError):
                mlr_min = 80
            use5 = (_gcv("GAME_5M_MULTIDAY_LR_REG_USE_5M_TAIL", "true") or "true").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            mlr_engine = None
            try:
                from report_generator import get_engine

                mlr_engine = get_engine()
            except Exception:
                pass
            mlr_fc = compute_log_return_multiday_forecast(
                ticker,
                volatility_5m_pct=out.get("volatility_5m_pct"),
                momentum_2h_pct=out.get("momentum_2h_pct"),
                period_days=mlr_period,
                ridge_lambda=mlr_lam,
                min_train_rows=mlr_min,
                use_intraday_features=use5,
                db_engine=mlr_engine,
            )
            if mlr_fc:
                out["log_return_multiday_forecast"] = mlr_fc
                out["log_return_multiday_forecast_summary"] = format_multiday_forecast_one_line(mlr_fc)
                hz_m = mlr_fc.get("horizons") or {}
                for hk, suffix in (("1", "1d"), ("2", "2d"), ("3", "3d")):
                    cell = hz_m.get(hk)
                    if not isinstance(cell, dict):
                        continue
                    pp = cell.get("predicted_pct_vs_spot")
                    trm = cell.get("train_rmse_log")
                    if pp is not None:
                        try:
                            out[f"multiday_lr_horizon_{suffix}_pct_vs_spot"] = round(float(pp), 3)
                        except (TypeError, ValueError):
                            pass
                    if trm is not None:
                        try:
                            out[f"multiday_lr_horizon_{suffix}_train_rmse_log"] = round(float(trm), 6)
                        except (TypeError, ValueError):
                            pass
                bs = (mlr_fc.get("bias_summary") or "").strip()
                if bs:
                    out["multiday_lr_bias"] = bs
                if mlr_fc.get("daily_last_date"):
                    out["multiday_lr_daily_last_date"] = mlr_fc.get("daily_last_date")
                if mlr_fc.get("method"):
                    out["multiday_lr_method"] = mlr_fc.get("method")
                if "premarket_db_used" in mlr_fc:
                    out["multiday_lr_premarket_db_used"] = bool(mlr_fc.get("premarket_db_used"))
                if "news_db_used" in mlr_fc:
                    out["multiday_lr_news_db_used"] = bool(mlr_fc.get("news_db_used"))
                if "macro_calendar_db_used" in mlr_fc:
                    out["multiday_lr_macro_calendar_db_used"] = bool(mlr_fc.get("macro_calendar_db_used"))
                if "symbol_calendar_db_used" in mlr_fc:
                    out["multiday_lr_symbol_calendar_db_used"] = bool(mlr_fc.get("symbol_calendar_db_used"))
                if "n_features" in mlr_fc:
                    out["multiday_lr_n_features"] = mlr_fc.get("n_features")
                dcs = mlr_fc.get("daily_close_source")
                if dcs:
                    out["multiday_lr_daily_close_source"] = str(dcs)
                mlr_append = (_gcv("GAME_5M_MULTIDAY_LR_REG_APPEND_REASONING", "false") or "false").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                )
                summ = out.get("log_return_multiday_forecast_summary")
                if mlr_append and isinstance(summ, str) and summ.strip():
                    reasoning = (reasoning + " [" + summ.strip() + "]").strip()
                    out["reasoning"] = reasoning
            else:
                out["multiday_lr_forecast_unavailable"] = True
        except Exception as e:
            logger.debug("log_return_multiday_forecast для %s: %s", ticker, e)
            out["multiday_lr_forecast_unavailable"] = True
            out["multiday_lr_forecast_error"] = str(e)[:240]

    # Совет по входу: ALLOW / CAUTION / AVOID — фильтр по новостям (KB), волатильности 5m и премаркет-гэпу.
    # Логика и согласование с чек-листом Квена (риск/ревард, мат. ожидание): docs/GAME_5M_WEB_CARDS.md
    p50_60 = p50_120 = None
    try:
        fc_h = (out.get("price_forecast_5m") or {}).get("horizons") or []
        for _h in fc_h:
            if not isinstance(_h, dict):
                continue
            m = _h.get("minutes")
            if m == 60:
                try:
                    p50_60 = float(_h.get("p50_pct_vs_spot"))
                except (TypeError, ValueError):
                    p50_60 = None
            elif m == 120:
                try:
                    p50_120 = float(_h.get("p50_pct_vs_spot"))
                except (TypeError, ValueError):
                    p50_120 = None
    except Exception:
        p50_60 = p50_120 = None
    strong_forecast = (
        isinstance(out.get("prob_up"), (int, float))
        and float(out.get("prob_up")) >= 0.58
        and isinstance(p50_60, (int, float))
        and isinstance(p50_120, (int, float))
        and p50_60 > 0
        and p50_120 > 0
    )

    entry_advice = "ALLOW"
    entry_advice_reason = ""
    if very_negative:
        if relax_very_neg:
            entry_advice = "CAUTION"
            entry_advice_reason = (
                "Сильный негатив в новостях; VIX ниже порога — рынок слабее дисконтирует панику; вход только с дисциплиной"
            )
        elif strong_forecast:
            entry_advice = "CAUTION"
            entry_advice_reason = "Есть сильный негатив в новостях, но прогноз 60/120 и prob_up поддерживают осторожный вход"
        else:
            entry_advice = "AVOID"
            entry_advice_reason = "Сильный негатив в новостях — вход отложен"
    elif volatility_5m_pct is not None and volatility_5m_pct > 1.0:
        entry_advice = "AVOID"
        entry_advice_reason = f"Высокая волатильность 5m ({volatility_5m_pct:.2f}%) — вход рискован"
    elif recent_negative or (volatility_5m_pct is not None and volatility_5m_pct > vol_wait_min):
        entry_advice = "CAUTION"
        if recent_negative:
            entry_advice_reason = "Негативные новости в базе — осторожность"
        else:
            entry_advice_reason = f"Волатильность 5m {volatility_5m_pct:.2f}% > порога {vol_wait_min:.2f}% — осторожность"
    elif session_phase == "PRE_MARKET" and premarket_context and premarket_context.get("premarket_gap_pct") is not None and premarket_context["premarket_gap_pct"] < -2:
        entry_advice = "CAUTION"
        entry_advice_reason = f"Премаркет: гэп {premarket_context['premarket_gap_pct']:+.2f}% — лучше войти после открытия или лимитом"
    entry_advice_reason_local = entry_advice_reason
    macro_risk: Dict[str, Any] = {}
    try:
        from services.macro_premarket_risk import apply_macro_to_entry_advice, evaluate_macro_premarket_risk

        macro_risk = evaluate_macro_premarket_risk()
        entry_advice, entry_advice_reason = apply_macro_to_entry_advice(
            entry_advice, entry_advice_reason, macro_risk
        )
    except Exception as e:
        logger.debug("macro_premarket_risk для %s: %s", ticker, e)
    if out.get("premarket_gap_pct") is None:
        try:
            from services.game5m_gap_forecast import load_frozen_gap_snapshot

            frozen_pm = load_frozen_gap_snapshot(ticker)
            if frozen_pm and frozen_pm.get("premarket_gap_pct") is not None:
                out["premarket_gap_pct"] = round(float(frozen_pm["premarket_gap_pct"]), 2)
        except Exception as e_fpm:
            logger.debug("frozen premarket_gap %s: %s", ticker, e_fpm)
    premarket_gap_baseline = None
    try:
        if out.get("premarket_gap_pct") is not None:
            from services.premarket_gap_baseline import evaluate_premarket_gap_baseline

            premarket_gap_baseline = evaluate_premarket_gap_baseline(
                out.get("premarket_gap_pct"),
                very_negative_news=bool(very_negative and not relax_very_neg),
                macro_risk_level=macro_risk.get("risk_level"),
                macro_equity_gap_bias=macro_risk.get("equity_gap_bias"),
                multiday_horizon_1d_pct=out.get("multiday_lr_horizon_1d_pct_vs_spot"),
            )
            if premarket_gap_baseline:
                pm_reason = str(premarket_gap_baseline.get("reason") or "")
                if premarket_gap_baseline.get("should_caution_entry") and entry_advice == "ALLOW":
                    entry_advice = "CAUTION"
                    entry_advice_reason = f"Premarket baseline: {pm_reason}"
                elif (
                    premarket_gap_baseline.get("should_boost_entry")
                    and entry_advice == "ALLOW"
                    and not entry_advice_reason
                ):
                    entry_advice_reason = f"Premarket baseline supports entry: {pm_reason}"
    except Exception as e:
        logger.debug("premarket_gap_baseline для %s: %s", ticker, e)
    if not entry_advice_reason and entry_advice == "ALLOW":
        entry_advice_reason = "Нет явных ограничений на вход"
    out["entry_advice"] = entry_advice
    out["entry_advice_reason"] = entry_advice_reason
    out["entry_advice_reason_local"] = entry_advice_reason_local
    if premarket_gap_baseline:
        out["premarket_gap_baseline"] = premarket_gap_baseline
        out["premarket_gap_baseline_signal"] = premarket_gap_baseline.get("signal")
        out["premarket_gap_baseline_action"] = premarket_gap_baseline.get("action")
        out["premarket_gap_baseline_reason"] = premarket_gap_baseline.get("reason")
        out["premarket_gap_take_watch"] = bool(premarket_gap_baseline.get("should_take_watch"))
    if macro_risk.get("enabled"):
        out["macro_risk_level"] = macro_risk.get("risk_level")
        out["macro_equity_gap_bias"] = macro_risk.get("equity_gap_bias")
        out["macro_risk_reasons"] = macro_risk.get("reasons")
        out["macro_indicators"] = {
            k: v.get("gap_pct") for k, v in (macro_risk.get("indicators") or {}).items()
        }
        try:
            from services.ticker_groups import get_market_ndx_ticker

            ndx_t = get_market_ndx_ticker()
            ndx_info = (macro_risk.get("indicators") or {}).get(ndx_t) or {}
            spy_info = (macro_risk.get("indicators") or {}).get("SPY") or {}
            if ndx_info.get("gap_pct") is not None:
                out["ndx_gap_pct"] = ndx_info.get("gap_pct")
            if spy_info.get("gap_pct") is not None:
                out["spy_gap_pct"] = spy_info.get("gap_pct")
            if ndx_info.get("premarket_last") is not None:
                out["ndx_premarket_last"] = ndx_info.get("premarket_last")
            if ndx_info.get("prev_close") is not None:
                out["ndx_prev_close"] = ndx_info.get("prev_close")
        except Exception as e_ndx:
            logger.debug("ndx macro fields для %s: %s", ticker, e_ndx)
        if macro_risk.get("macro_predicted_sector_gap_pct") is not None:
            out["macro_predicted_sector_gap_pct"] = macro_risk.get("macro_predicted_sector_gap_pct")
            out["macro_sector_proxy"] = macro_risk.get("macro_sector_proxy")
    try:
        from services.ticker_open_gap_predict import attach_ticker_open_gap_fields

        attach_ticker_open_gap_fields(out, ticker=ticker, macro_risk=macro_risk or None)
    except Exception as e_tog:
        logger.debug("ticker_open_gap_predict для %s: %s", ticker, e_tog)
    try:
        from services.game5m_forecast_layer import attach_game5m_forecast_layer

        attach_game5m_forecast_layer(out)
    except Exception as e_fl:
        logger.debug("game5m_forecast_layer для %s: %s", ticker, e_fl)
    # Рекомендованный вход и ожидаемая прибыль при достижении цели (если цель/цена доступны).
    try:
        p_now = out.get("price") or price
        p_now = float(p_now) if p_now is not None else None
    except (TypeError, ValueError):
        p_now = None
    tp_price = out.get("suggested_take_profit_price")
    if p_now is not None and p_now > 0:
        out["entry_price_recommended"] = round(p_now, 2)
        try:
            vol_for_band = float(out.get("volatility_5m_pct")) if out.get("volatility_5m_pct") is not None else None
        except (TypeError, ValueError):
            vol_for_band = None
        band_pct = 0.5 if vol_for_band is None else max(0.25, min(1.2, vol_for_band * 0.8))
        out["entry_price_range_low"] = round(p_now * (1.0 - band_pct / 100.0), 2)
        out["entry_price_range_high"] = round(p_now * (1.0 + band_pct / 100.0), 2)
        try:
            if tp_price is not None and float(tp_price) > p_now:
                out["expected_profit_pct_if_take"] = round((float(tp_price) / p_now - 1.0) * 100.0, 2)
        except (TypeError, ValueError):
            pass

    # Дополнительный guard качества входа: по чек-листу R:R и матожиданию (конфигурируемо, по умолчанию выключено).
    # Это ранний фильтр против сценариев "BUY -> не дошёл до тейка -> TIME_EXIT с убытком".
    try:
        from config_loader import get_config_value as _gcv_guard

        use_guard = (_gcv_guard("GAME_5M_ENTRY_QUALITY_GUARD_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        rr_min_raw = (_gcv_guard("GAME_5M_ENTRY_QUALITY_MIN_RR", "1.2") or "1.2").strip()
        ev_min_raw = (_gcv_guard("GAME_5M_ENTRY_QUALITY_MIN_EV_PCT", "0.0") or "0.0").strip()
        rr_min = float(rr_min_raw)
        ev_min = float(ev_min_raw)
    except Exception:
        use_guard = False
        rr_min = 1.2
        ev_min = 0.0

    if use_guard:
        rr = None
        ev = None
        up = out.get("estimated_upside_pct_day")
        down = out.get("estimated_downside_pct_day")
        if isinstance(up, (int, float)) and isinstance(down, (int, float)) and float(down) > 0:
            rr = float(up) / float(down)
        pu = out.get("prob_up")
        pdn = out.get("prob_down")
        if (
            isinstance(pu, (int, float))
            and isinstance(pdn, (int, float))
            and isinstance(up, (int, float))
            and isinstance(down, (int, float))
        ):
            ev = float(pu) * float(up) - float(pdn) * float(down)

        bad_rr = rr is not None and rr < rr_min
        bad_ev = ev is not None and ev <= ev_min
        if decision in ("BUY", "STRONG_BUY") and (bad_rr or bad_ev):
            old_decision = decision
            decision = "HOLD"
            reason_parts: List[str] = []
            if bad_rr and rr is not None:
                reason_parts.append(f"R:R {rr:.2f} < {rr_min:.2f}")
            if bad_ev and ev is not None:
                reason_parts.append(f"EV {ev:+.2f}% <= {ev_min:+.2f}%")
            guard_msg = "entry-quality guard: " + ", ".join(reason_parts)
            reasons.append(guard_msg)
            reasoning = (reasoning + " " + guard_msg).strip()
            out["decision"] = decision
            out["reasoning"] = reasoning
            out["entry_quality_guard_triggered"] = True
            out["entry_quality_guard_prev_decision"] = old_decision
            out["entry_quality_guard_reason"] = guard_msg
        else:
            out["entry_quality_guard_triggered"] = False

    # Блок «LLM-новости» в решении 5m: по умолчанию выключен (GAME_5M_USE_LLM_NEWS=false).
    # Текущий источник — ответ модели по обучению, не в реальном времени; даты в тексте могут быть старыми.
    # Включать true имеет смысл только при наличии актуального источника (RAG по KB, web search API). См. docs/GAME_5M_NEWS.md.
    if use_llm_news:
        try:
            from config_loader import get_config_value
            use_in_5m = get_config_value("GAME_5M_USE_LLM_NEWS", "false").strip().lower() in ("1", "true", "yes")
            if use_in_5m and get_config_value("USE_LLM_NEWS", "").strip().lower() in ("1", "true", "yes"):
                from services.llm_service import get_llm_service
                llm = get_llm_service()
                llm_data = llm.fetch_news_for_ticker(ticker) if getattr(llm, "client", None) else None
                if llm_data:
                    out["llm_news_content"] = llm_data.get("content")
                    out["llm_sentiment"] = llm_data.get("sentiment_score")
                    out["llm_insight"] = llm_data.get("insight")
                    if llm_data.get("llm_comparison"):
                        out["llm_comparison"] = llm_data["llm_comparison"]
        except Exception as e:
            logger.debug("LLM новости перед решением %s: %s", ticker, e)

    # CatBoost + multiday: в decision_stack при DECISION_STACK_OWN_FINALIZE=true (фаза 3).
    try:
        from services.decision_stack.game5m_policy import stack_own_finalize_enabled

        own_stack = stack_own_finalize_enabled()
    except Exception:
        own_stack = False
    if not own_stack:
        try:
            from config_loader import get_config_value

            cb_on = (get_config_value("GAME_5M_CATBOOST_ENABLED", "false") or "false").strip().lower() in (
                "1",
                "true",
                "yes",
            )
        except Exception:
            cb_on = False
        if cb_on:
            try:
                from services.catboost_5m_signal import attach_catboost_signal

                attach_catboost_signal(out, ticker)
            except Exception as e:
                logger.warning("attach_catboost_signal(%s): %s", ticker, e)
            try:
                from services.catboost_5m_signal import finalize_technical_decision_with_catboost

                finalize_technical_decision_with_catboost(out)
            except Exception as e:
                logger.warning("finalize_technical_decision_with_catboost(%s): %s", ticker, e)
                out.setdefault("technical_decision_core", out.get("decision"))
                out.setdefault("technical_decision_effective", out.get("decision"))
                out.setdefault("catboost_fusion_mode", "none")
        else:
            out.setdefault("technical_decision_core", out.get("decision"))
            out.setdefault("technical_decision_effective", out.get("decision"))
            out.setdefault("catboost_fusion_mode", "none")

        try:
            from services.multiday_lr_gate import finalize_technical_decision_with_multiday

            finalize_technical_decision_with_multiday(out)
        except Exception as e:
            logger.warning("finalize_technical_decision_with_multiday(%s): %s", ticker, e)

    dec_eff = out.get("decision")
    branch = out.get("technical_entry_branch")
    if branch and dec_eff in ("BUY", "STRONG_BUY"):
        expl = _build_5m_entry_explanation(
            branch=str(branch),
            decision=str(dec_eff),
            decision_rule_params=out.get("decision_rule_params") or {},
            feats=out,
            strong_buy_downgraded=bool(out.get("entry_strong_buy_downgraded")),
        )
        out["entry_condition"] = expl["condition"]
        out["entry_intuition"] = expl["intuition"]
    else:
        out["entry_condition"] = None
        out["entry_intuition"] = None

    try:
        from services.catboost_5m_signal import attach_catboost_bar_v2_signal

        attach_catboost_bar_v2_signal(out, ticker)
    except Exception as e:
        logger.warning("attach_catboost_bar_v2_signal(%s): %s", ticker, e)

    try:
        from services.decision_stack import finalize_game5m_decision_stack

        finalize_game5m_decision_stack(out, ticker=ticker, kb_news=kb_news)
    except Exception as e:
        logger.warning("finalize_game5m_decision_stack(%s): %s", ticker, e)

    return out


def get_5m_technical_signal(
    ticker: str,
    days: int = None,
    use_llm_news: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Технический сигнал 5m: подмножество get_decision_5m для signal5m и выравнивания с cron.
    Те же правила, что в scripts/send_sndk_signal_cron.py; без длинных списков корреляций и новостей.
    """
    d5 = get_decision_5m(ticker, days=days, use_llm_news=use_llm_news)
    if d5 is None:
        return None
    return {k: d5.get(k) for k in TECHNICAL_SIGNAL_KEYS if k in d5}


def build_5m_compact_payload(d5: Dict[str, Any]) -> Dict[str, Any]:
    """
    Компактный payload для recommend5m: технический вывод + краткий LLM вывод при наличии.
    Без длинных списков корреляций и полных текстов новостей.
    """
    out = {k: d5.get(k) for k in TECHNICAL_SIGNAL_KEYS if k in d5}
    if d5.get("llm_correlation_reasoning"):
        out["llm_reasoning"] = (d5.get("llm_correlation_reasoning") or "")[:500]
    if d5.get("llm_key_factors"):
        out["llm_key_factors"] = d5.get("llm_key_factors")
    if d5.get("llm_news_content"):
        out["llm_news_preview"] = (d5.get("llm_news_content") or "")[:300]
    if d5.get("llm_sentiment") is not None:
        out["llm_sentiment"] = d5.get("llm_sentiment")
    return out

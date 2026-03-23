"""
Расчёт рекомендуемых параметров игры 5m по данным 5m свечей: потолок тейка (PCT) и макс. дней (DAYS).

Используется диагностическими скриптами suggest_take_profit_caps_5m.py
и suggest_max_position_days_5m.py для ручного анализа.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def compute_take_profit_suggestions(
    tickers: List[str],
    n_sessions: int = 7,
    fetch_days: int = 10,
) -> Dict[str, Dict[str, Any]]:
    """
    По каждой сессии: макс. рост от open до high сессии (%). По тикеру: медиана, p70, p80.
    Предлагаемый потолок = округлённый p70 (2–10%).
    Возвращает dict[ticker] = {suggested_pct, median_pct, p70, p80, n_sessions, current_config}.
    """
    import numpy as np
    from services.recommend_5m import fetch_5m_ohlc, filter_to_last_n_us_sessions
    from config_loader import get_config_value

    result: Dict[str, Dict[str, Any]] = {}
    for ticker in tickers:
        df = fetch_5m_ohlc(ticker, days=fetch_days)
        if df is None or df.empty:
            result[ticker] = {"error": "нет 5m данных"}
            continue
        df = filter_to_last_n_us_sessions(df, n=n_sessions)
        if df is None or df.empty or "_session" not in df.columns:
            if "datetime" in df.columns:
                dt = df["datetime"]
                if hasattr(dt.dt, "date"):
                    df = df.copy()
                    df["_session"] = dt.dt.date
                else:
                    df["_session"] = dt.dt.normalize()
            else:
                result[ticker] = {"error": "нет сессий после фильтра"}
                continue
        sessions = df.groupby("_session", sort=False)
        up_pcts = []
        for _sess, grp in sessions:
            grp = grp.sort_values("datetime")
            open_ = float(grp["Open"].iloc[0])
            high = float(grp["High"].max())
            if open_ <= 0:
                continue
            up_from_open = (high - open_) / open_ * 100.0
            up_pcts.append(up_from_open)
        if not up_pcts:
            result[ticker] = {"error": "0 сессий с данными"}
            continue
        arr = np.array(up_pcts)
        median_pct = float(np.median(arr))
        p70 = float(np.percentile(arr, 70))
        p80 = float(np.percentile(arr, 80))
        suggested = round(p70 * 2) / 2.0
        suggested = max(2.0, min(10.0, suggested))
        key = f"GAME_5M_TAKE_PROFIT_PCT_{ticker.upper()}"
        current = get_config_value(key, "").strip()
        if not current:
            current = get_config_value("GAME_5M_TAKE_PROFIT_PCT", "7").strip()
        result[ticker] = {
            "suggested_pct": suggested,
            "median_pct": median_pct,
            "p70": p70,
            "p80": p80,
            "n_sessions": len(up_pcts),
            "current_config": current,
        }
    return result


def compute_max_days_suggestions(
    tickers: List[str],
    n_sessions: int = 25,
    take_pct_by_ticker: Optional[Dict[str, float]] = None,
    fetch_days: int = 35,
) -> Dict[str, Dict[str, Any]]:
    """
    Для каждого «входа» (open сессии S): на какой день T впервые high[S..T] >= open_S * (1 + take_pct/100).
    take_pct_by_ticker: подставляемый потолок тейка (%; если None — из конфига через _take_profit_cap_pct).
    Возвращает dict[ticker] = {suggested_days, median_days, p70, p80, n_sessions, current_config}.
    """
    import numpy as np
    from services.recommend_5m import fetch_5m_ohlc, filter_to_last_n_us_sessions
    from config_loader import get_config_value

    if take_pct_by_ticker is None:
        from services.game_5m import _take_profit_cap_pct
        take_pct_by_ticker = {t: _take_profit_cap_pct(t) for t in tickers}

    result: Dict[str, Dict[str, Any]] = {}
    for ticker in tickers:
        take_pct = take_pct_by_ticker.get(ticker) or 7.0
        df = fetch_5m_ohlc(ticker, days=fetch_days)
        if df is None or df.empty:
            result[ticker] = {"error": "нет 5m данных"}
            continue
        df = filter_to_last_n_us_sessions(df, n=n_sessions)
        if df is None or df.empty or "_session" not in df.columns:
            if "datetime" in df.columns:
                dt = df["datetime"]
                if hasattr(dt.dt, "date"):
                    df = df.copy()
                    df["_session"] = dt.dt.date
                else:
                    df["_session"] = dt.dt.normalize()
            else:
                result[ticker] = {"error": "нет сессий"}
                continue
        sessions_order = sorted(df["_session"].unique())
        if len(sessions_order) < 2:
            result[ticker] = {"error": "мало сессий"}
            continue
        days_to_reach = []
        for i, sess in enumerate(sessions_order):
            grp = df[df["_session"] == sess].sort_values("datetime")
            if grp.empty:
                continue
            open_s = float(grp["Open"].iloc[0])
            if open_s <= 0:
                continue
            target = open_s * (1 + take_pct / 100.0)
            running_high = open_s
            for j in range(i, len(sessions_order)):
                grp_j = df[df["_session"] == sessions_order[j]]
                if grp_j.empty:
                    continue
                running_high = max(running_high, float(grp_j["High"].max()))
                if running_high >= target:
                    days_to_reach.append(j - i + 1)
                    break
        if not days_to_reach:
            key = f"GAME_5M_MAX_POSITION_DAYS_{ticker.upper()}"
            cur = get_config_value(key, "").strip() or get_config_value("GAME_5M_MAX_POSITION_DAYS", "1")
            result[ticker] = {"suggested_days": None, "current_config": cur, "error": "тейк не достигнут за окно"}
            continue
        arr = np.array(days_to_reach)
        median_d = float(np.median(arr))
        p70_d = float(np.percentile(arr, 70))
        p80_d = float(np.percentile(arr, 80))
        suggested = min(7, max(1, int(np.ceil(p80_d))))
        key = f"GAME_5M_MAX_POSITION_DAYS_{ticker.upper()}"
        current = get_config_value(key, "").strip()
        if not current:
            current = get_config_value("GAME_5M_MAX_POSITION_DAYS", "1").strip()
        result[ticker] = {
            "suggested_days": suggested,
            "median_days": median_d,
            "p70": p70_d,
            "p80": p80_d,
            "n_sessions": len(sessions_order),
            "current_config": current,
        }
    return result

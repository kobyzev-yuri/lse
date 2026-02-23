"""
Рекомендация по 5-минутным данным с учётом 5-дневной статистики.

Используется для агрессивной игры на быстрых движениях (например SNDK):
- загрузка 5m данных за последние 1–7 дней через yfinance;
- RSI и волатильность по 5m;
- краткосрочный импульс (например за последние 2 часа);
- решение BUY/HOLD/SELL и параметры управления (стоп/тейк уже под интрадей).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Максимум дней 5m по ограничениям Yahoo
MAX_DAYS_5M = 7
# Период RSI по 5m свечам (14 свечей ≈ 70 мин)
RSI_PERIOD_5M = 14
# Баров в «2 часа» для импульса
BARS_2H = 24


def fetch_5m_ohlc(ticker: str, days: int = 5) -> Optional[pd.DataFrame]:
    """
    Загружает 5-минутные OHLC за последние days дней (явный диапазон до «сейчас»).

    Returns:
        DataFrame с колонками datetime, Open, High, Low, Close или None.
    """
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
    df = t.history(start=start_str, end=end_str, interval="5m", auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df.rename_axis("datetime").reset_index()
    for c in ("Open", "High", "Low", "Close"):
        if c not in df.columns:
            return None
    return df


def _log_returns(series: pd.Series) -> pd.Series:
    """Лог-доходности по правилам проекта."""
    return np.log(series / series.shift(1)).dropna()


def compute_rsi_5m(closes: pd.Series, period: int = RSI_PERIOD_5M) -> Optional[float]:
    """RSI по ряду 5m закрытий (последнее значение = текущее)."""
    from services.rsi_calculator import compute_rsi_from_closes
    vals = closes.dropna().tolist()
    if len(vals) < period + 1:
        return None
    return compute_rsi_from_closes(vals, period=period)


def get_decision_5m(ticker: str, days: int = 5) -> Optional[Dict[str, Any]]:
    """
    Строит рекомендацию по 5m данным за последние days дней с учётом 5-дневной статистики.

    Returns:
        dict с ключами:
        - decision: "BUY" | "STRONG_BUY" | "HOLD" | "SELL"
        - reasoning: краткое обоснование
        - price: последняя цена
        - rsi_5m: RSI по 5m
        - volatility_5m_pct: волатильность (std лог-доходностей за период, в %)
        - momentum_2h_pct: изменение цены за последние ~2ч (%)
        - high_5d, low_5d: макс/мин за период
        - period_str: текст диапазона дат
        - stop_loss_pct, take_profit_pct: для интрадей (уже уже чем дневные)
        - bars_count: число 5m баров
    """
    df = fetch_5m_ohlc(ticker, days=days)
    if df is None or df.empty or len(df) < RSI_PERIOD_5M + 1:
        logger.warning("Недостаточно 5m данных для %s за %d дн.", ticker, days)
        return None

    df = df.sort_values("datetime").reset_index(drop=True)
    closes = df["Close"].astype(float)
    high_5d = float(df["High"].max())
    low_5d = float(df["Low"].min())
    price = float(closes.iloc[-1])

    # Лог-доходности за весь период
    log_ret = _log_returns(closes)
    volatility_5m_pct = float(log_ret.std() * 100) if len(log_ret) > 1 else 0.0

    # RSI по 5m
    rsi_5m = compute_rsi_5m(closes, period=RSI_PERIOD_5M)

    # Импульс за последние ~2 часа (24 свечи по 5m)
    n = min(BARS_2H, len(closes) - 1)
    price_2h_ago = float(closes.iloc[-(n + 1)])
    momentum_2h_pct = ((price / price_2h_ago) - 1.0) * 100.0 if price_2h_ago > 0 else 0.0

    dt_min = df["datetime"].min()
    dt_max = df["datetime"].max()
    if hasattr(dt_min, "strftime"):
        period_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}"
    else:
        period_str = f"{dt_min} – {dt_max}"

    # Правила решения (агрессивные под интрадей)
    decision = "HOLD"
    reasons = []

    if rsi_5m is not None:
        if rsi_5m <= 32 and momentum_2h_pct >= -0.3:
            decision = "STRONG_BUY"
            reasons.append(f"RSI(5m)={rsi_5m:.1f} — перепроданность, отскок")
        elif rsi_5m <= 38 and price <= low_5d * 1.005:
            decision = "BUY"
            reasons.append(f"RSI(5m)={rsi_5m:.1f}, цена у 5д минимума")
        elif rsi_5m >= 76:
            decision = "SELL"
            reasons.append(f"RSI(5m)={rsi_5m:.1f} — перекупленность")
        elif rsi_5m >= 68:
            if decision == "HOLD":
                reasons.append(f"RSI(5m)={rsi_5m:.1f} — ближе к перекупленности, ждать")
        elif momentum_2h_pct > 0.5 and (rsi_5m is None or rsi_5m < 62):
            if decision == "HOLD":
                decision = "BUY"
                reasons.append(f"импульс +{momentum_2h_pct:.2f}% за 2ч, RSI не перекуплен")

    if volatility_5m_pct > 0.4 and decision in ("BUY", "STRONG_BUY"):
        reasons.append(f"волатильность 5m высокая ({volatility_5m_pct:.2f}%) — предпочтительны лимитные ордера")
    elif volatility_5m_pct > 0.6:
        if decision == "HOLD":
            reasons.append(f"волатильность 5m {volatility_5m_pct:.2f}% — выжидать")

    if not reasons:
        reasons.append(
            f"5m: цена {price:.2f}, RSI={rsi_5m or '—'}, импульс 2ч={momentum_2h_pct:+.2f}%, волатильность={volatility_5m_pct:.2f}%"
        )

    reasoning = " ".join(reasons)

    # Параметры под интрадей (уже стоп/тейк)
    stop_loss_pct = 2.5
    take_profit_pct = 5.0

    return {
        "decision": decision,
        "reasoning": reasoning,
        "price": price,
        "rsi_5m": rsi_5m,
        "volatility_5m_pct": volatility_5m_pct,
        "momentum_2h_pct": momentum_2h_pct,
        "high_5d": high_5d,
        "low_5d": low_5d,
        "period_str": period_str,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "bars_count": len(df),
    }

"""
Краткосрочный прогноз цены по 5m барам (интрадей).

Модель: лог-доходности за 5m ~ i.i.d. N(μ, σ²) на коротком окне (оценка μ, σ по недавним барам).
Суммарная лог-доходность за H баров ~ N(H·μ, H·σ²) → квантили цены и P(цена > spot).

Не заменяет исполнение игры; для риск-дисклеймера см. docs/GAME_5M_PRICE_FORECAST.md
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# Горизонты по умолчанию (минуты), кратны 5m бару
DEFAULT_HORIZONS_MIN: Tuple[int, int, int] = (30, 60, 120)
BAR_MINUTES = 5

# Квантили нормального распределения (без scipy)
Z_P10 = -1.2815515655446004
Z_P50 = 0.0
Z_P90 = 1.2815515655446004


def _log_returns_series(closes: pd.Series) -> np.ndarray:
    c = closes.astype(float).replace(0, np.nan).dropna()
    if len(c) < 2:
        return np.array([])
    lr = np.log(c / c.shift(1)).dropna().values
    return lr[np.isfinite(lr)]


def _phi(x: float) -> float:
    """CDF стандартной нормали через math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def compute_price_forecast_5m(
    closes: pd.Series,
    spot: float,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MIN,
    bar_minutes: int = BAR_MINUTES,
) -> Optional[Dict[str, Any]]:
    """
    Прогноз уровней цены на горизонтах (по умолчанию 30/60/120 мин).

    Args:
        closes: ряд закрытий 5m (хронологический порядок).
        spot: текущая цена (последнее закрытие или премаркет last).
        horizons_minutes: список горизонтов в минутах (должны делиться на bar_minutes).

    Returns:
        dict с ключами spot, method, horizons (список), или None при недостаточных данных.
    """
    if spot is None or not math.isfinite(float(spot)) or float(spot) <= 0:
        return None
    lr = _log_returns_series(closes)
    if len(lr) < 3:
        return None

    s0 = float(spot)
    log_s0 = math.log(s0)

    # σ и μ по недавнему окну (лог-доходности в долях, не в %)
    tail = min(80, len(lr))
    lr_tail = lr[-tail:]
    sigma_bar = float(np.std(lr_tail, ddof=1)) if len(lr_tail) >= 2 else float(np.std(lr_tail))
    if not math.isfinite(sigma_bar) or sigma_bar < 1e-8:
        sigma_bar = max(float(np.std(lr)), 1e-6)

    k_mu = min(24, len(lr_tail))
    mu_bar = float(np.mean(lr_tail[-k_mu:])) if k_mu >= 1 else 0.0
    if not math.isfinite(mu_bar):
        mu_bar = 0.0
    # Умеренное сжатие дрейфа (интрадей переоценка тренда ведёт к широким хвостам)
    mu_bar = max(-0.002, min(0.002, mu_bar))

    horizons_out: List[Dict[str, Any]] = []
    for hm in horizons_minutes:
        try:
            hmin = int(hm)
        except (TypeError, ValueError):
            continue
        if hmin <= 0 or hmin % bar_minutes != 0:
            continue
        h_bars = hmin // bar_minutes
        mu_h = mu_bar * h_bars
        sig_h = sigma_bar * math.sqrt(float(h_bars))
        if sig_h < 1e-10:
            sig_h = 1e-10

        log_p10 = log_s0 + mu_h + Z_P10 * sig_h
        log_p50 = log_s0 + mu_h + Z_P50 * sig_h
        log_p90 = log_s0 + mu_h + Z_P90 * sig_h
        p10 = math.exp(log_p10)
        p50 = math.exp(log_p50)
        p90 = math.exp(log_p90)

        # P(S_T > S0) при лог-нормальной сумме
        z = (math.sqrt(float(h_bars)) * mu_bar) / sigma_bar if sigma_bar > 0 else 0.0
        p_higher_than_spot = _phi(z)

        horizons_out.append(
            {
                "minutes": hmin,
                "bars": h_bars,
                "p10_price": round(p10, 4),
                "p50_price": round(p50, 4),
                "p90_price": round(p90, 4),
                "p10_pct_vs_spot": round((p10 / s0 - 1.0) * 100.0, 3),
                "p50_pct_vs_spot": round((p50 / s0 - 1.0) * 100.0, 3),
                "p90_pct_vs_spot": round((p90 / s0 - 1.0) * 100.0, 3),
                "p_price_gt_spot": round(p_higher_than_spot, 3),
                "sigma_5m_log": round(sigma_bar, 6),
                "drift_5m_log_mean": round(mu_bar, 8),
            }
        )

    if not horizons_out:
        return None

    return {
        "spot": round(s0, 4),
        "method": "lognormal_iid_5m",
        "bar_minutes": bar_minutes,
        "window_log_returns": len(lr_tail),
        "horizons": horizons_out,
    }


def format_price_forecast_one_line(fc: Optional[Dict[str, Any]], max_horizons: int = 3) -> str:
    """Краткая строка для Telegram/карточки."""
    if not fc or not fc.get("horizons"):
        return ""
    parts = []
    for h in (fc.get("horizons") or [])[:max_horizons]:
        m = h.get("minutes")
        lo = h.get("p10_pct_vs_spot")
        mid = h.get("p50_pct_vs_spot")
        hi = h.get("p90_pct_vs_spot")
        ph = h.get("p_price_gt_spot")
        if m is None:
            continue
        parts.append(
            f"{m}м: [{lo:+.2f}% … {mid:+.2f}% … {hi:+.2f}%] P(>spot)≈{ph}"
        )
    return " | ".join(parts)

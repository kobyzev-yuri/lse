"""
Аппроксимация хвоста графика для пролонгации в зону прогноза.
Поддерживаемые методы: linear (МНК прямая), quadratic (парабола), ema (рекомендуется для волатильных рядов).
"""

import numpy as np
from typing import Literal

Method = Literal["linear", "quadratic", "ema"]


def _ema_series(prices: np.ndarray, span: int) -> np.ndarray:
    """Экспоненциальная скользящая средняя: alpha = 2/(span+1). Первое значение — prices[0]."""
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(prices, dtype=float)
    out[0] = float(prices[0])
    for i in range(1, len(prices)):
        out[i] = alpha * float(prices[i]) + (1.0 - alpha) * out[i - 1]
    return out


def fit_and_prolong(
    closes: np.ndarray,
    method: Method = "ema",
    prolong_bars: int = 12,
) -> dict:
    """
    Аппроксимирует последние точки Close и экстраполирует на prolong_bars вперёд.

    Аргументы:
        closes: массив цен Close (последние N свечей).
        method: "linear" — прямая МНК, "quadratic" — парабола МНК, "ema" — наклон по EMA (для волатильных рядов).
        prolong_bars: на сколько баров вперёд экстраполировать.

    Возвращает:
        slope_per_bar: оценка наклона (цена/бар) в последней точке.
        end_price: значение аппроксиманта в точке (n - 1 + prolong_bars).
        curve_bar_offsets: [0, 1, ..., prolong_bars].
        curve_prices: цены на кривой для отрисовки.
    """
    n = len(closes)
    if n < 2:
        slope = 0.0
        end_price = float(closes[-1]) if n else 0.0
        return {
            "slope_per_bar": slope,
            "end_price": end_price,
            "curve_bar_offsets": list(range(prolong_bars + 1)),
            "curve_prices": [end_price] * (prolong_bars + 1),
        }

    y = np.asarray(closes, dtype=float)

    if method == "ema":
        # Менее агрессивная аппроксимация: EMA по всему окну (span = n), чтобы наклон не «махал» на каждом откате
        span = max(2, n)
        ema = _ema_series(y, span)
        slope_per_bar = (ema[-1] - ema[0]) / (n - 1) if n > 1 else 0.0
        last_price = float(y[-1])
        curve_bar_offsets = list(range(prolong_bars + 1))
        curve_prices = [last_price + slope_per_bar * k for k in curve_bar_offsets]
        end_price = curve_prices[-1]
        return {
            "slope_per_bar": slope_per_bar,
            "end_price": end_price,
            "curve_bar_offsets": curve_bar_offsets,
            "curve_prices": curve_prices,
        }

    x = np.arange(n, dtype=float)
    if method == "linear":
        coeffs = np.polyfit(x, y, 1)
        slope_per_bar = float(coeffs[0])
        def eval_at(bar_offset: float) -> float:
            return np.polyval(coeffs, n - 1 + bar_offset)
    elif method == "quadratic":
        coeffs = np.polyfit(x, y, 2)
        slope_per_bar = float(2 * coeffs[0] * (n - 1) + coeffs[1])
        def eval_at(bar_offset: float) -> float:
            return np.polyval(coeffs, n - 1 + bar_offset)
    else:
        raise ValueError(f"Unknown method: {method!r}")

    end_price = float(eval_at(prolong_bars))
    curve_bar_offsets = list(range(prolong_bars + 1))
    curve_prices = [float(eval_at(k)) for k in curve_bar_offsets]

    return {
        "slope_per_bar": slope_per_bar,
        "end_price": end_price,
        "curve_bar_offsets": curve_bar_offsets,
        "curve_prices": curve_prices,
    }

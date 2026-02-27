"""
Стратегия «ловля отскока» после геополитического падения.

После резкого падения на геополитических новостях (≥2% за сессию)
рынок часто даёт отскок в течение 1–3 сессий. Long на отскоке часто
хорошо отрабатывает (реализация страха уже произошла).
"""

from typing import Dict, Any, List
from .base_strategy import BaseStrategy


class GeopoliticalBounceStrategy(BaseStrategy):
    """
    Стратегия входа в long на отскоке после геополитического падения.

    Подходит когда:
    - Предыдущая сессия упала на ≥2% (prev_day_return_pct <= -2.0)
    - Повышенная волатильность (опционально, для фильтра)
    - Решение: допускать BUY на первом отскоке со стопом под минимум паники
    """

    def __init__(self):
        super().__init__("Geopolitical Bounce")

    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,  # -1.0 до 1.0
    ) -> bool:
        """Стратегия подходит при падении предыдущей сессии ≥2% и повышенной волатильности."""
        prev_day_return_pct = technical_data.get("prev_day_return_pct")
        volatility_5 = technical_data.get("volatility_5")
        avg_volatility_20 = technical_data.get("avg_volatility_20")

        if prev_day_return_pct is None:
            return False

        # Триггер: вчерашняя сессия упала минимум на 2%
        sharp_drop = prev_day_return_pct <= -2.0

        # Опционально: повышенная волатильность (стресс на рынке); если нет данных — не требуем
        elevated_vol = True
        if (
            volatility_5 is not None
            and avg_volatility_20 is not None
            and avg_volatility_20 > 0
        ):
            elevated_vol = volatility_5 >= avg_volatility_20 * 1.05

        return sharp_drop and elevated_vol

    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,  # -1.0 до 1.0
    ) -> Dict[str, Any]:
        """Сигнал: BUY на отскоке (long), стоп под локальный минимум."""
        close = float(technical_data.get("close", 0))
        prev_day_return_pct = technical_data.get("prev_day_return_pct")
        current_day_return_pct = technical_data.get("current_day_return_pct")

        # Базовый сигнал: long на отскоке
        signal = "BUY"
        # Уверенность выше, если сегодня уже зелёная свеча (отскок начался)
        if current_day_return_pct is not None and current_day_return_pct > 0:
            confidence = min(0.85, 0.6 + current_day_return_pct / 50)  # отскок в процессе
        elif prev_day_return_pct is not None:
            confidence = min(0.8, 0.5 + abs(prev_day_return_pct) / 20)  # чем сильнее падение, тем выше потенциал отскока
        else:
            confidence = 0.65

        # Стоп уже под «минимумом паники» (вчерашний минимум или текущий low — в бэктесте нет low, используем консервативно)
        entry_price = close
        stop_loss = 5.0  # 5% под точку входа (упрощённо; в реальности — под локальный low)
        take_profit = 4.0  # 4% тейк-профит на отскоке

        prev_str = f"{prev_day_return_pct:.2f}%" if prev_day_return_pct is not None else "N/A"
        reasoning = (
            f"Geopolitical Bounce: предыдущая сессия {prev_str}, "
            f"допускаем long на отскоке, стоп под минимум паники (~{stop_loss}%), тейк {take_profit}%"
        )

        insight = None
        for n in news_data or []:
            if n.get("insight"):
                insight = n.get("insight")
                break

        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy": self.name,
            "insight": insight,
        }

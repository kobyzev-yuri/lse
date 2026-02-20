"""
Стратегия «нейтральный режим» — используется только когда ни одна другая не подходит.
Не выбирается по is_suitable(); выдаёт консервативный HOLD.
"""

from typing import Dict, Any, List
from .base_strategy import BaseStrategy


class NeutralStrategy(BaseStrategy):
    """
    Базовый/нейтральный режим: нет явного тренда, гэпа или экстремального sentiment.
    Рекомендация — удержание до появления чётких условий для другой стратегии.
    """

    def __init__(self):
        super().__init__("Neutral")

    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,
    ) -> bool:
        """Никогда не выбирается по условиям — только как fallback из менеджера."""
        return False

    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,
    ) -> Dict[str, Any]:
        """Консервативный сигнал: HOLD, т.к. режим рынка не определён."""
        close = technical_data.get("close")
        reasoning = (
            "Режим не определён (нет явного тренда, гэпа или экстремального sentiment). "
            "Рекомендация — удержание."
        )
        # Сильный отрицательный sentiment может понизить до осторожного HOLD/SELL
        if sentiment_score < -0.5:
            return {
                "signal": "HOLD",
                "confidence": 0.4,
                "reasoning": reasoning + " Отрицательный sentiment — осторожность.",
                "entry_price": float(close) if close is not None else None,
                "stop_loss": None,
                "take_profit": None,
                "insight": None,
            }
        if sentiment_score > 0.5:
            return {
                "signal": "HOLD",
                "confidence": 0.5,
                "reasoning": reasoning + " Положительный sentiment, но недостаточно для входа.",
                "entry_price": float(close) if close is not None else None,
                "stop_loss": None,
                "take_profit": None,
                "insight": None,
            }
        return {
            "signal": "HOLD",
            "confidence": 0.4,
            "reasoning": reasoning,
            "entry_price": float(close) if close is not None else None,
            "stop_loss": None,
            "take_profit": None,
            "insight": None,
        }

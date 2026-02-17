"""
Стратегия возврата к среднему (Mean Reversion)
Подходит для стабильных активов (например, MSFT)
"""

from typing import Dict, Any, List
from .base_strategy import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """
    Стратегия возврата к среднему (Mean Reversion)
    
    Подходит когда:
    - Цена значительно отклонилась от среднего
    - Высокая волатильность
    - Рынок перекуплен или перепродан
    - Новости нейтральные или противоречивые
    """
    
    def __init__(self):
        super().__init__("Mean Reversion")
    
    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> bool:
        """Проверяет условия для Mean Reversion стратегии"""
        close = technical_data.get('close')
        sma_5 = technical_data.get('sma_5')
        volatility_5 = technical_data.get('volatility_5')
        avg_volatility_20 = technical_data.get('avg_volatility_20')
        
        if not all([close, sma_5, volatility_5, avg_volatility_20]):
            return False
        
        # Условия для Mean Reversion:
        # 1. Значительное отклонение от среднего (более 2%)
        price_deviation = abs((close - sma_5) / sma_5) * 100 if sma_5 > 0 else 0
        significant_deviation = price_deviation > 2.0
        
        # 2. Высокая волатильность
        high_volatility = volatility_5 > avg_volatility_20 * 1.2
        
        # 3. Нейтральный sentiment (не слишком экстремальный)
        # В центрированной шкале: -0.2 до 0.2 = нейтральный
        neutral_sentiment = -0.4 < sentiment_score < 0.4
        
        # Mean Reversion подходит при значительном отклонении и высокой волатильности
        return significant_deviation and (high_volatility or neutral_sentiment)
    
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> Dict[str, Any]:
        """Вычисляет сигнал для Mean Reversion стратегии"""
        close = float(technical_data.get('close', 0))
        sma_5 = float(technical_data.get('sma_5', 0))
        volatility_5 = float(technical_data.get('volatility_5', 0))
        avg_volatility_20 = float(technical_data.get('avg_volatility_20', 0))
        
        # Расчет отклонения от среднего
        price_deviation = ((close - sma_5) / sma_5) * 100 if sma_5 > 0 else 0
        
        # Определение сигнала (торгуем против отклонения)
        # Применяем sentiment: положительный sentiment ослабляет сигнал продажи
        if price_deviation < -3.0:  # Цена значительно ниже среднего - покупаем
            base_signal = "BUY"
            base_confidence = min(0.85, 0.5 + abs(price_deviation) / 10)
            # Положительный sentiment усиливает сигнал покупки
            confidence = min(0.9, base_confidence * (1.0 + sentiment_score))
            signal = "STRONG_BUY" if confidence > 0.75 else "BUY"
        elif price_deviation > 3.0:  # Цена значительно выше среднего - продаем
            base_signal = "SELL"
            base_confidence = min(0.85, 0.5 + abs(price_deviation) / 10)
            # Отрицательный sentiment усиливает сигнал продажи
            confidence = min(0.9, base_confidence * (1.0 - sentiment_score))
            signal = "SELL" if confidence > 0.6 else "HOLD"
        elif abs(price_deviation) > 2.0:
            signal = "BUY" if price_deviation < 0 else "HOLD"
            confidence = 0.6
        else:
            signal = "HOLD"
            confidence = 0.3
        
        # Рекомендуемые параметры
        entry_price = close
        stop_loss = 5.0  # 5% стоп-лосс для Mean Reversion (более широкий)
        take_profit = 4.0  # 4% тейк-профит (ожидаем возврат к среднему)
        
        # Извлекаем insight из новостей
        insight = self._extract_insight(news_data)
        
        reasoning = (
            f"Mean Reversion стратегия: цена {close:.2f} отклонена от SMA_5 {sma_5:.2f} "
            f"на {price_deviation:.2f}%, волатильность высокая "
            f"({volatility_5:.2f} > {avg_volatility_20:.2f}), ожидаем возврат к среднему, "
            f"sentiment {sentiment_score:.2f}"
        )
        
        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy": self.name,
            "insight": insight
        }
    
    def _extract_insight(self, news_data: List[Dict[str, Any]]) -> str:
        """Извлекает ключевой факт из новостей"""
        if not news_data:
            return None
        
        for news in news_data:
            if news.get('insight'):
                return news.get('insight')
        
        return None




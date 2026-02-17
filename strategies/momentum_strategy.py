"""
Стратегия следования тренду (Momentum)
Подходит для активов с быстрыми движениями (например, SNDK)
"""

from typing import Dict, Any, List
from .base_strategy import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """
    Стратегия следования тренду (Momentum)
    
    Подходит когда:
    - Явный восходящий тренд (цена выше SMA)
    - Низкая волатильность относительно среднего
    - Положительный sentiment новостей
    - Стабильный тренд без резких колебаний
    """
    
    def __init__(self):
        super().__init__("Momentum")
    
    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> bool:
        """Проверяет условия для Momentum стратегии"""
        close = technical_data.get('close')
        sma_5 = technical_data.get('sma_5')
        volatility_5 = technical_data.get('volatility_5')
        avg_volatility_20 = technical_data.get('avg_volatility_20')
        
        if not all([close, sma_5, volatility_5, avg_volatility_20]):
            return False
        
        # Условия для Momentum:
        # 1. Цена выше SMA (восходящий тренд)
        price_above_sma = close > sma_5
        
        # 2. Низкая волатильность (стабильный тренд)
        low_volatility = volatility_5 < avg_volatility_20
        
        # 3. Положительный sentiment (опционально, но желательно)
        positive_sentiment = sentiment_score > 0.0  # В центрированной шкале 0.0 = нейтральный
        
        # Momentum подходит если есть тренд и низкая волатильность
        return price_above_sma and low_volatility
    
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> Dict[str, Any]:
        """Вычисляет сигнал для Momentum стратегии"""
        close = float(technical_data.get('close', 0))
        sma_5 = float(technical_data.get('sma_5', 0))
        volatility_5 = float(technical_data.get('volatility_5', 0))
        avg_volatility_20 = float(technical_data.get('avg_volatility_20', 0))
        
        # Расчет силы тренда
        price_deviation = ((close - sma_5) / sma_5) * 100 if sma_5 > 0 else 0
        
        # Базовый сигнал на основе технических данных
        if price_deviation > 2.0:
            base_signal = "STRONG_BUY"
            base_confidence = min(0.9, 0.6 + (price_deviation / 10))
        elif price_deviation > 1.0:
            base_signal = "BUY"
            base_confidence = min(0.8, 0.5 + (price_deviation / 10))
        else:
            base_signal = "HOLD"
            base_confidence = 0.4
        
        # Применяем sentiment (умножение на центрированную шкалу)
        # sentiment > 0 усиливает сигнал, sentiment < 0 ослабляет
        from utils.sentiment_utils import apply_sentiment_to_signal
        
        adjusted_confidence = apply_sentiment_to_signal(base_confidence, sentiment_score)
        
        # Определяем финальный сигнал с учетом sentiment
        if adjusted_confidence > 0.7 and sentiment_score > 0.3:
            signal = "STRONG_BUY"
            confidence = min(0.95, adjusted_confidence)
        elif adjusted_confidence > 0.5 and sentiment_score > 0.0:
            signal = "BUY"
            confidence = min(0.85, adjusted_confidence)
        elif adjusted_confidence < -0.3 or sentiment_score < -0.5:
            signal = "SELL"
            confidence = min(0.8, abs(adjusted_confidence))
        else:
            signal = "HOLD"
            confidence = 0.4
        
        # Рекомендуемые параметры
        entry_price = close
        stop_loss = 3.0  # 3% стоп-лосс для Momentum
        take_profit = 8.0  # 8% тейк-профит для Momentum
        
        # Извлекаем insight из новостей
        insight = self._extract_insight(news_data)
        
        reasoning = (
            f"Momentum стратегия: цена {close:.2f} выше SMA_5 {sma_5:.2f} "
            f"(отклонение {price_deviation:.2f}%), волатильность низкая "
            f"({volatility_5:.2f} < {avg_volatility_20:.2f}), sentiment {sentiment_score:.2f}"
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
        
        # Берем первую новость с insight, если есть
        for news in news_data:
            if news.get('insight'):
                return news.get('insight')
        
        return None




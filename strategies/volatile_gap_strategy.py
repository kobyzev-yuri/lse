"""
Стратегия для волатильных рынков с гэпами
Подходит для "вертолетов" - активов с резкими движениями
"""

from typing import Dict, Any, List
from .base_strategy import BaseStrategy


class VolatileGapStrategy(BaseStrategy):
    """
    Стратегия для волатильных рынков с гэпами
    
    Подходит когда:
    - Очень высокая волатильность
    - Большие ценовые гэпы (цена открытия сильно отличается от закрытия)
    - Важные новости (макро-события)
    - Неопределенность на рынке
    """
    
    def __init__(self):
        super().__init__("Volatile Gap")
    
    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> bool:
        """Проверяет условия для VolatileGap стратегии"""
        volatility_5 = technical_data.get('volatility_5')
        avg_volatility_20 = technical_data.get('avg_volatility_20')
        open_price = technical_data.get('open_price')
        close = technical_data.get('close')
        
        if not volatility_5 or not avg_volatility_20:
            return False
        
        # Условия для VolatileGap:
        # 1. Очень высокая волатильность (более чем в 1.5 раза выше среднего)
        very_high_volatility = volatility_5 > avg_volatility_20 * 1.5
        
        # 2. Большой гэп (если есть данные об открытии)
        has_gap = False
        if open_price and close:
            gap_percent = abs((close - open_price) / open_price) * 100 if open_price > 0 else 0
            has_gap = gap_percent > 3.0  # Гэп более 3%
        
        # 3. Есть важные новости (макро-события или много новостей)
        has_macro_news = any(
            news.get('ticker') in ['MACRO', 'US_MACRO'] 
            for news in news_data
        )
        many_news = len(news_data) >= 3
        
        # 4. Экстремальный sentiment (очень положительный или очень отрицательный)
        extreme_sentiment = abs(sentiment_score) > 0.6  # В центрированной шкале
        
        return very_high_volatility and (has_gap or has_macro_news or many_news or extreme_sentiment)
    
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> Dict[str, Any]:
        """Вычисляет сигнал для VolatileGap стратегии"""
        close = float(technical_data.get('close', 0))
        open_price = technical_data.get('open_price')
        volatility_5 = float(technical_data.get('volatility_5', 0))
        avg_volatility_20 = float(technical_data.get('avg_volatility_20', 0))
        
        # Расчет гэпа
        gap_percent = 0.0
        if open_price and close:
            gap_percent = ((close - open_price) / open_price) * 100 if open_price > 0 else 0
        
        volatility_ratio = volatility_5 / avg_volatility_20 if avg_volatility_20 > 0 else 1.0
        
        # В волатильных условиях ориентируемся на sentiment и гэп
        # Применяем sentiment напрямую (умножение)
        if sentiment_score > 0.6 and volatility_ratio > 1.5:
            signal = "STRONG_BUY"
            confidence = min(0.9, 0.6 + (sentiment_score - 0.6) * 2 + (volatility_ratio - 1.5) * 0.2)
        elif sentiment_score > 0.3:
            signal = "BUY"
            confidence = 0.7
        elif sentiment_score < -0.3:
            signal = "SELL"
            confidence = 0.7
        elif gap_percent > 5.0:  # Большой положительный гэп
            signal = "BUY"
            confidence = min(0.8, 0.5 + gap_percent / 20)
        elif gap_percent < -5.0:  # Большой отрицательный гэп
            signal = "SELL"
            confidence = min(0.8, 0.5 + abs(gap_percent) / 20)
        else:
            signal = "HOLD"
            confidence = 0.4
        
        # Рекомендуемые параметры (более широкие для волатильности)
        entry_price = close
        stop_loss = 7.0  # 7% стоп-лосс для волатильных рынков
        take_profit = 12.0  # 12% тейк-профит (высокий потенциал)
        
        # Извлекаем insight из новостей
        insight = self._extract_insight(news_data)
        
        reasoning = (
            f"VolatileGap стратегия: очень высокая волатильность "
            f"({volatility_5:.2f} vs средняя {avg_volatility_20:.2f}, "
            f"коэффициент {volatility_ratio:.2f}), гэп {gap_percent:.2f}%, "
            f"sentiment {sentiment_score:.2f}, новостей {len(news_data)}"
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


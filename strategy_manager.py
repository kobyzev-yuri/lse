"""
Интеллектуальный диспетчер стратегий
Выбирает оптимальную стратегию на основе режима рынка
"""

import logging
from typing import Dict, Any, List, Optional
from strategies.momentum_strategy import MomentumStrategy
from strategies.mean_reversion_strategy import MeanReversionStrategy
from strategies.volatile_gap_strategy import VolatileGapStrategy
from strategies.neutral_strategy import NeutralStrategy
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Интеллектуальный диспетчер для выбора оптимальной стратегии
    на основе волатильности, sentiment и ценовых гэпов
    """
    
    def __init__(self):
        """Инициализация всех доступных стратегий"""
        self.strategies = [
            MomentumStrategy(),
            MeanReversionStrategy(),
            VolatileGapStrategy()
        ]
        
        # Пороги для принятия решений
        self.high_volatility_threshold = 1.5  # Коэффициент волатильности
        self.extreme_sentiment_threshold = 0.6  # В центрированной шкале (-1.0 до 1.0)
        self.gap_threshold = 3.0  # Процент гэпа
        
        logger.info(f"✅ StrategyManager инициализирован с {len(self.strategies)} стратегиями")
    
    def select_strategy(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0 (центрированная шкала)
    ) -> Optional[BaseStrategy]:
        """
        Выбирает оптимальную стратегию на основе режима рынка
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (close, sma_5, volatility_5, avg_volatility_20, open_price)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment в центрированной шкале (-1.0 до 1.0)
            
        Returns:
            Выбранная стратегия или None если ни одна не подходит
        """
        volatility_5 = technical_data.get('volatility_5')
        avg_volatility_20 = technical_data.get('avg_volatility_20')
        open_price = technical_data.get('open_price')
        close = technical_data.get('close')
        
        # Расчет коэффициента волатильности
        volatility_ratio = 1.0
        if volatility_5 and avg_volatility_20 and avg_volatility_20 > 0:
            volatility_ratio = volatility_5 / avg_volatility_20
        
        # Расчет гэпа (если есть данные об открытии)
        gap_percent = 0.0
        if open_price and close and open_price > 0:
            gap_percent = abs((close - open_price) / open_price) * 100
        
        logger.info(f"📊 Анализ режима рынка для {ticker}:")
        logger.info(f"   Волатильность: {volatility_ratio:.2f}x (порог: {self.high_volatility_threshold})")
        logger.info(f"   Sentiment: {sentiment_score:.2f} (порог: ±{self.extreme_sentiment_threshold})")
        logger.info(f"   Гэп: {gap_percent:.2f}% (порог: {self.gap_threshold}%)")
        
        # Логика выбора стратегии (The Switch)
        
        # 1. VolatileGapStrategy: очень высокая волатильность + гэп или экстремальный sentiment
        if volatility_ratio > self.high_volatility_threshold:
            if gap_percent > self.gap_threshold or abs(sentiment_score) > self.extreme_sentiment_threshold:
                selected = self._get_strategy_by_name("Volatile Gap")
                if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                    logger.info(f"🔄 Volatility is high ({volatility_ratio:.2f}x), Sentiment is Extreme ({sentiment_score:.2f}) -> Switching to VolatileGapStrategy for {ticker}")
                    return selected
                else:
                    logger.info(f"   ⚠️ VolatileGap не подходит для {ticker} (is_suitable вернул False)")
        
        # 2. MomentumStrategy: низкая волатильность + положительный sentiment
        if volatility_ratio < 1.0 and sentiment_score > 0.3:
            selected = self._get_strategy_by_name("Momentum")
            if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(f"🔄 Market is calm (volatility={volatility_ratio:.2f}x), Positive sentiment ({sentiment_score:.2f}) -> Using MomentumStrategy for {ticker}")
                return selected
            else:
                logger.info(f"   ⚠️ Momentum не подходит для {ticker} (is_suitable вернул False)")
        
        # 3. MeanReversionStrategy: высокая волатильность + нейтральный sentiment
        if volatility_ratio > 1.2 and abs(sentiment_score) < 0.4:
            selected = self._get_strategy_by_name("Mean Reversion")
            if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(f"🔄 Market is volatile ({volatility_ratio:.2f}x), Neutral sentiment ({sentiment_score:.2f}) -> Using MeanReversionStrategy for {ticker}")
                return selected
            else:
                logger.info(f"   ⚠️ MeanReversion не подходит для {ticker} (is_suitable вернул False)")
        
        # 4. Fallback: проверяем все стратегии и выбираем первую подходящую
        for strategy in self.strategies:
            if strategy.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(f"✅ Выбрана стратегия: {strategy.name} (fallback)")
                return strategy
        
        # 5. Нейтральный режим: ни одна стратегия не подошла — консервативный HOLD
        default_strategy = NeutralStrategy()
        logger.info(
            f"📋 Условия не подходят ни под одну стратегию → используется {default_strategy.name} (удержание)"
        )
        return default_strategy
    
    def _get_strategy_by_name(self, name: str) -> Optional[BaseStrategy]:
        """Возвращает стратегию по имени"""
        for strategy in self.strategies:
            if strategy.name == name:
                return strategy
        return None
    
    def get_all_strategies(self) -> List[BaseStrategy]:
        """Возвращает список всех доступных стратегий"""
        return self.strategies


# Глобальный экземпляр менеджера
_strategy_manager: Optional[StrategyManager] = None


def get_strategy_manager() -> StrategyManager:
    """Получить глобальный экземпляр менеджера стратегий"""
    global _strategy_manager
    if _strategy_manager is None:
        _strategy_manager = StrategyManager()
    return _strategy_manager


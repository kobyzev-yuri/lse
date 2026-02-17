"""
Фабрика стратегий для торговли
Паттерн Strategy для выбора оптимальной торговой стратегии
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging
import pandas as pd

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Базовый класс для всех торговых стратегий
    """
    
    def __init__(self, name: str):
        """
        Args:
            name: Название стратегии
        """
        self.name = name
    
    @abstractmethod
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> Dict[str, Any]:
        """
        Вычисляет торговый сигнал на основе данных
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (close, sma_5, volatility_5, avg_volatility_20)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score
            
        Returns:
            dict с сигналом и метаданными:
            {
                "signal": "BUY" | "STRONG_BUY" | "HOLD" | "SELL",
                "confidence": 0.0-1.0,
                "reasoning": "обоснование",
                "entry_price": float | None,
                "stop_loss": float | None,
                "take_profit": float | None
            }
        """
        pass
    
    @abstractmethod
    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> bool:
        """
        Проверяет, подходит ли стратегия для текущих условий
        
        Args:
            technical_data: Технические данные
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score
            
        Returns:
            True если стратегия подходит, False иначе
        """
        pass
    
    def __str__(self):
        return f"{self.__class__.__name__}({self.name})"


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
        sentiment_score: float
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
        positive_sentiment = sentiment_score > 0.5
        
        # Momentum подходит если есть тренд и низкая волатильность
        return price_above_sma and low_volatility
    
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> Dict[str, Any]:
        """Вычисляет сигнал для Momentum стратегии"""
        close = float(technical_data.get('close', 0))
        sma_5 = float(technical_data.get('sma_5', 0))
        volatility_5 = float(technical_data.get('volatility_5', 0))
        avg_volatility_20 = float(technical_data.get('avg_volatility_20', 0))
        
        # Расчет силы тренда
        price_deviation = ((close - sma_5) / sma_5) * 100 if sma_5 > 0 else 0
        
        # Определение сигнала
        if price_deviation > 2.0 and sentiment_score > 0.6:
            signal = "STRONG_BUY"
            confidence = min(0.9, 0.6 + (price_deviation / 10) + (sentiment_score - 0.6))
        elif price_deviation > 1.0 and sentiment_score > 0.5:
            signal = "BUY"
            confidence = min(0.8, 0.5 + (price_deviation / 10) + (sentiment_score - 0.5))
        else:
            signal = "HOLD"
            confidence = 0.4
        
        # Рекомендуемые параметры
        entry_price = close
        stop_loss = 3.0  # 3% стоп-лосс для Momentum
        take_profit = 8.0  # 8% тейк-профит для Momentum
        
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
            "strategy": self.name
        }


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
        sentiment_score: float
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
        
        # 3. Нейтральный или противоречивый sentiment (не слишком экстремальный)
        neutral_sentiment = 0.3 < sentiment_score < 0.7
        
        # Mean Reversion подходит при значительном отклонении и высокой волатильности
        return significant_deviation and (high_volatility or neutral_sentiment)
    
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> Dict[str, Any]:
        """Вычисляет сигнал для Mean Reversion стратегии"""
        close = float(technical_data.get('close', 0))
        sma_5 = float(technical_data.get('sma_5', 0))
        volatility_5 = float(technical_data.get('volatility_5', 0))
        avg_volatility_20 = float(technical_data.get('avg_volatility_20', 0))
        
        # Расчет отклонения от среднего
        price_deviation = ((close - sma_5) / sma_5) * 100 if sma_5 > 0 else 0
        
        # Определение сигнала (торгуем против отклонения)
        if price_deviation < -3.0:  # Цена значительно ниже среднего - покупаем
            signal = "BUY"
            confidence = min(0.85, 0.5 + abs(price_deviation) / 10)
        elif price_deviation > 3.0:  # Цена значительно выше среднего - продаем
            signal = "SELL"
            confidence = min(0.85, 0.5 + abs(price_deviation) / 10)
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
        
        reasoning = (
            f"Mean Reversion стратегия: цена {close:.2f} отклонена от SMA_5 {sma_5:.2f} "
            f"на {price_deviation:.2f}%, волатильность высокая "
            f"({volatility_5:.2f} > {avg_volatility_20:.2f}), ожидаем возврат к среднему"
        )
        
        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy": self.name
        }


class VolatileGapStrategy(BaseStrategy):
    """
    Стратегия для волатильных рынков с гэпами
    
    Подходит когда:
    - Очень высокая волатильность
    - Большие ценовые гэпы
    - Важные новости (макро-события)
    - Неопределенность на рынке
    """
    
    def __init__(self):
        super().__init__("Volatile Gap")
    
    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> bool:
        """Проверяет условия для VolatileGap стратегии"""
        volatility_5 = technical_data.get('volatility_5')
        avg_volatility_20 = technical_data.get('avg_volatility_20')
        
        if not volatility_5 or not avg_volatility_20:
            return False
        
        # Условия для VolatileGap:
        # 1. Очень высокая волатильность (более чем в 1.5 раза выше среднего)
        very_high_volatility = volatility_5 > avg_volatility_20 * 1.5
        
        # 2. Есть важные новости (макро-события или много новостей)
        has_macro_news = any(
            news.get('ticker') in ['MACRO', 'US_MACRO'] 
            for news in news_data
        )
        many_news = len(news_data) >= 3
        
        # 3. Экстремальный sentiment (очень положительный или очень отрицательный)
        extreme_sentiment = sentiment_score > 0.8 or sentiment_score < 0.2
        
        return very_high_volatility and (has_macro_news or many_news or extreme_sentiment)
    
    def calculate_signal(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> Dict[str, Any]:
        """Вычисляет сигнал для VolatileGap стратегии"""
        close = float(technical_data.get('close', 0))
        volatility_5 = float(technical_data.get('volatility_5', 0))
        avg_volatility_20 = float(technical_data.get('avg_volatility_20', 0))
        
        # В волатильных условиях ориентируемся на sentiment
        volatility_ratio = volatility_5 / avg_volatility_20 if avg_volatility_20 > 0 else 1.0
        
        # Определение сигнала на основе sentiment и волатильности
        if sentiment_score > 0.7 and volatility_ratio > 1.5:
            signal = "STRONG_BUY"
            confidence = min(0.9, 0.6 + (sentiment_score - 0.7) * 2)
        elif sentiment_score > 0.6:
            signal = "BUY"
            confidence = 0.7
        elif sentiment_score < 0.3:
            signal = "SELL"
            confidence = 0.7
        else:
            signal = "HOLD"
            confidence = 0.4
        
        # Рекомендуемые параметры (более широкие для волатильности)
        entry_price = close
        stop_loss = 7.0  # 7% стоп-лосс для волатильных рынков
        take_profit = 12.0  # 12% тейк-профит (высокий потенциал)
        
        reasoning = (
            f"VolatileGap стратегия: очень высокая волатильность "
            f"({volatility_5:.2f} vs средняя {avg_volatility_20:.2f}, "
            f"коэффициент {volatility_ratio:.2f}), sentiment {sentiment_score:.2f}, "
            f"новостей {len(news_data)}"
        )
        
        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy": self.name
        }


class StrategyFactory:
    """
    Фабрика для выбора оптимальной стратегии на основе условий рынка
    """
    
    def __init__(self):
        """Инициализация всех доступных стратегий"""
        self.strategies = [
            MomentumStrategy(),
            MeanReversionStrategy(),
            VolatileGapStrategy()
        ]
        logger.info(f"✅ StrategyFactory инициализирован с {len(self.strategies)} стратегиями")
    
    def select_strategy(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> Optional[BaseStrategy]:
        """
        Выбирает наиболее подходящую стратегию на основе условий
        
        Args:
            technical_data: Технические данные
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score
            
        Returns:
            Выбранная стратегия или None если ни одна не подходит
        """
        # Проверяем все стратегии и выбираем подходящие
        suitable_strategies = [
            strategy for strategy in self.strategies
            if strategy.is_suitable(technical_data, news_data, sentiment_score)
        ]
        
        if not suitable_strategies:
            logger.info("⚠️ Ни одна стратегия не подходит для текущих условий")
            return None
        
        # Если несколько стратегий подходят, выбираем первую (можно добавить приоритеты)
        selected = suitable_strategies[0]
        logger.info(f"✅ Выбрана стратегия: {selected.name}")
        
        return selected
    
    def get_all_strategies(self) -> List[BaseStrategy]:
        """Возвращает список всех доступных стратегий"""
        return self.strategies
    
    def get_strategy_by_name(self, name: str) -> Optional[BaseStrategy]:
        """Возвращает стратегию по имени"""
        for strategy in self.strategies:
            if strategy.name == name:
                return strategy
        return None


# Глобальный экземпляр фабрики
_strategy_factory: Optional[StrategyFactory] = None


def get_strategy_factory() -> StrategyFactory:
    """Получить глобальный экземпляр фабрики стратегий"""
    global _strategy_factory
    if _strategy_factory is None:
        _strategy_factory = StrategyFactory()
    return _strategy_factory




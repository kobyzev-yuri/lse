"""
Базовый класс для всех торговых стратегий
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging

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
        sentiment_score: float  # Теперь -1.0 до 1.0 (центрированная шкала)
    ) -> Dict[str, Any]:
        """
        Вычисляет торговый сигнал на основе данных
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (close, sma_5, volatility_5, avg_volatility_20, open_price)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score (-1.0 до 1.0, центрированная шкала)
            
        Returns:
            dict с сигналом и метаданными:
            {
                "signal": "BUY" | "STRONG_BUY" | "HOLD" | "SELL",
                "confidence": 0.0-1.0,
                "reasoning": "обоснование",
                "entry_price": float | None,
                "stop_loss": float | None,
                "take_profit": float | None,
                "insight": "ключевой факт из новостей" | None
            }
        """
        pass
    
    @abstractmethod
    def is_suitable(
        self,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0
    ) -> bool:
        """
        Проверяет, подходит ли стратегия для текущих условий
        
        Args:
            technical_data: Технические данные
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score (-1.0 до 1.0)
            
        Returns:
            True если стратегия подходит, False иначе
        """
        pass
    
    def __str__(self):
        return f"{self.__class__.__name__}({self.name})"




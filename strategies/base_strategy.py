"""
Базовый класс для всех торговых стратегий
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import logging
import re

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
    
    def get_parameters(self, default_params: Dict[str, Any], target_identifier: str = 'GLOBAL') -> Dict[str, Any]:
        """
        Загружает параметры стратегии:
        1) кодовые дефолты стратегии;
        2) PORTFOLIO_<STRATEGY>_*_PCT из config.env / web /parameters;
        3) strategy_parameters из БД (самый высокий приоритет).
        """
        merged = default_params.copy()
        merged.update(self._get_config_params())

        from utils.parameter_store import get_parameter_store
        
        store = get_parameter_store()
        db_params = store.get_parameters(self.name, target_identifier)
        if db_params:
            merged.update(db_params)
            
        return merged

    def _config_prefix(self) -> str:
        """Momentum -> MOMENTUM, Mean Reversion -> MEAN_REVERSION."""
        return re.sub(r"[^A-Z0-9]+", "_", self.name.upper()).strip("_")

    def _get_config_params(self) -> Dict[str, float]:
        """Read portfolio strategy stop/take defaults exposed in config.env and /parameters."""
        try:
            from config_loader import get_config_value
        except Exception:
            return {}

        prefix = self._config_prefix()
        mapping = {
            "stop_loss": f"PORTFOLIO_{prefix}_STOP_LOSS_PCT",
            "take_profit": f"PORTFOLIO_{prefix}_TAKE_PROFIT_PCT",
        }
        out: Dict[str, float] = {}
        for param_name, env_key in mapping.items():
            raw = (get_config_value(env_key, "") or "").strip()
            if not raw:
                continue
            try:
                out[param_name] = float(raw)
            except (TypeError, ValueError):
                logger.warning("Некорректное значение %s=%r, игнорируем", env_key, raw)
        return out
    
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




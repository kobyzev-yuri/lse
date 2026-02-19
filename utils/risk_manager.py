"""
Модуль для управления risk limits и capacity компании
Все данные хранятся локально и НЕ попадают в git
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Менеджер рисков для управления лимитами компании
    
    Загружает конфигурацию из local/risk_limits.json
    """
    
    def __init__(self, risk_config_path: Optional[Path] = None):
        """
        Инициализация RiskManager
        
        Args:
            risk_config_path: Путь к файлу risk_limits.json (если None, ищет в local/)
        """
        if risk_config_path is None:
            # Ищем в local/risk_limits.json
            project_root = Path(__file__).parent.parent
            risk_config_path = project_root / "local" / "risk_limits.json"
        
        self.config_path = Path(risk_config_path)
        self.config: Dict = {}
        self._load_config()
    
    def _load_config(self):
        """Загружает конфигурацию risk limits"""
        if not self.config_path.exists():
            logger.warning(
                f"⚠️ Файл risk_limits.json не найден: {self.config_path}\n"
                f"   Создайте его на основе local/risk_limits.example.json"
            )
            # Используем дефолтные значения
            self.config = self._get_default_config()
            return
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            logger.info(f"✅ Загружены risk limits из {self.config_path}")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки risk_limits.json: {e}")
            self.config = self._get_default_config()
    
    def _get_default_config(self) -> Dict:
        """Возвращает дефолтную конфигурацию (консервативные лимиты)"""
        return {
            "risk_capacity": {
                "total_capital_usd": 100000.0,
                "max_position_size_usd": 10000.0,
                "max_portfolio_exposure_percent": 80.0,
                "max_single_ticker_exposure_percent": 20.0,
                "max_daily_loss_usd": 5000.0,
                "max_daily_loss_percent": 5.0,
                "max_drawdown_percent": 15.0
            },
            "position_limits": {
                "max_positions_open": 10,
                "min_position_size_usd": 1000.0,
                "max_position_concentration_percent": 20.0
            },
            "risk_parameters": {
                "stop_loss_percent": 5.0,
                "take_profit_percent": 10.0,
                "max_leverage": 1.0
            }
        }
    
    def get_max_position_size(self, ticker: Optional[str] = None) -> float:
        """
        Возвращает максимальный размер позиции в USD
        
        Args:
            ticker: Тикер (для будущего расширения - разные лимиты по тикерам)
            
        Returns:
            Максимальный размер позиции в USD
        """
        return self.config.get("risk_capacity", {}).get("max_position_size_usd", 10000.0)
    
    def get_max_portfolio_exposure(self) -> float:
        """Возвращает максимальную экспозицию портфеля в процентах"""
        return self.config.get("risk_capacity", {}).get("max_portfolio_exposure_percent", 80.0)
    
    def get_max_single_ticker_exposure(self) -> float:
        """Возвращает максимальную экспозицию по одному тикеру в процентах"""
        return self.config.get("risk_capacity", {}).get("max_single_ticker_exposure_percent", 20.0)
    
    def get_max_daily_loss(self) -> Dict[str, float]:
        """
        Возвращает максимальные дневные потери
        
        Returns:
            dict с 'usd' и 'percent'
        """
        capacity = self.config.get("risk_capacity", {})
        return {
            "usd": capacity.get("max_daily_loss_usd", 5000.0),
            "percent": capacity.get("max_daily_loss_percent", 5.0)
        }
    
    def get_max_positions_open(self) -> int:
        """Возвращает максимальное количество открытых позиций"""
        return self.config.get("position_limits", {}).get("max_positions_open", 10)
    
    def get_stop_loss_percent(self) -> float:
        """Возвращает процент стоп-лосса"""
        return self.config.get("risk_parameters", {}).get("stop_loss_percent", 5.0)
    
    def get_take_profit_percent(self) -> float:
        """Возвращает процент тейк-профита"""
        return self.config.get("risk_parameters", {}).get("take_profit_percent", 10.0)
    
    def get_total_capital(self) -> float:
        """Возвращает общий капитал компании"""
        return self.config.get("risk_capacity", {}).get("total_capital_usd", 100000.0)
    
    def check_position_size(self, position_size_usd: float, ticker: str = None) -> tuple[bool, str]:
        """
        Проверяет, не превышает ли размер позиции лимиты
        
        Args:
            position_size_usd: Размер позиции в USD
            ticker: Тикер (опционально)
            
        Returns:
            tuple: (is_valid, error_message)
        """
        max_size = self.get_max_position_size(ticker)
        
        if position_size_usd > max_size:
            return False, f"Размер позиции {position_size_usd:.2f} USD превышает лимит {max_size:.2f} USD"
        
        min_size = self.config.get("position_limits", {}).get("min_position_size_usd", 1000.0)
        if position_size_usd < min_size:
            return False, f"Размер позиции {position_size_usd:.2f} USD меньше минимума {min_size:.2f} USD"
        
        return True, ""
    
    def check_portfolio_exposure(self, current_exposure_usd: float, new_position_usd: float) -> tuple[bool, str]:
        """
        Проверяет, не превышает ли новая позиция лимит экспозиции портфеля
        
        Args:
            current_exposure_usd: Текущая экспозиция портфеля в USD
            new_position_usd: Размер новой позиции в USD
            
        Returns:
            tuple: (is_valid, error_message)
        """
        total_capital = self.get_total_capital()
        new_exposure = current_exposure_usd + new_position_usd
        exposure_percent = (new_exposure / total_capital) * 100.0
        
        max_exposure = self.get_max_portfolio_exposure()
        
        if exposure_percent > max_exposure:
            return False, (
                f"Экспозиция портфеля {exposure_percent:.2f}% превышает лимит {max_exposure:.2f}% "
                f"(текущая: {current_exposure_usd:.2f} USD, новая позиция: {new_position_usd:.2f} USD)"
            )
        
        return True, ""
    
    def check_daily_loss(self, daily_loss_usd: float, daily_loss_percent: float) -> tuple[bool, str]:
        """
        Проверяет, не превышены ли дневные потери
        
        Args:
            daily_loss_usd: Потери за день в USD
            daily_loss_percent: Потери за день в процентах
            
        Returns:
            tuple: (is_valid, error_message)
        """
        limits = self.get_max_daily_loss()
        
        if daily_loss_usd > limits["usd"]:
            return False, f"Дневные потери {daily_loss_usd:.2f} USD превышают лимит {limits['usd']:.2f} USD"
        
        if daily_loss_percent > limits["percent"]:
            return False, f"Дневные потери {daily_loss_percent:.2f}% превышают лимит {limits['percent']:.2f}%"
        
        return True, ""
    
    def get_broker_info(self) -> Dict:
        """Возвращает информацию о брокере"""
        return self.config.get("broker_limits", {}).get("swiss_bank_name", {})
    
    def get_exchange_info(self) -> Dict:
        """Возвращает информацию о бирже (NYSE)"""
        return self.config.get("exchange_requirements", {}).get("NYSE", {})
    
    def is_trading_hours(self) -> bool:
        """
        Проверяет, находятся ли мы в торговые часы NYSE
        
        Returns:
            True если торговые часы, False иначе
        """
        exchange_info = self.get_exchange_info()
        if not exchange_info:
            return True  # Если нет информации, разрешаем торговлю
        
        # TODO: Реализовать проверку торговых часов с учетом timezone
        # Пока возвращаем True
        return True


# Глобальный экземпляр (singleton)
_risk_manager: Optional[RiskManager] = None


def get_risk_manager() -> RiskManager:
    """
    Получает глобальный экземпляр RiskManager (singleton)
    
    Returns:
        RiskManager instance
    """
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager
